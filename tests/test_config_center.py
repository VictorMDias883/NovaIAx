"""
Tests for the admin "Configurações" config center.

Scope (logic + critical flows only):

* plan-ceiling overrides reaching :func:`get_plan_limits` and reverting on
  reset, including the "blank = inherit" semantics;
* feature-flag evaluation and the public app-config payload shape;
* maintenance mode surfacing in ``GET /api/v1/app-config``;
* the app-config HTTP cache (ETag revalidation);
* the AI provider enable/priority policy singleton.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from app.clients.ai_metrics import ai_metrics, provider_policy
from app.clients.ai_types import PlanTier
from app.clients.plan_limits import get_plan_limits
from app.clients.plan_limits_overrides import plan_limit_overrides
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models.ai_provider_setting import AIProviderSetting
from app.models.app_config import AppConfig
from app.models.plan_limit_override import PlanLimitOverride
from app.repositories.ai_provider_setting_repository import AIProviderSettingRepository
from app.services.ai_ops_service import refresh_provider_policy
from app.services.app_config_service import AppConfigService
from app.services.plan_limit_override_service import PlanLimitOverrideService
from fastapi.testclient import TestClient
from sqlalchemy import delete

#: Config keys this module writes, deleted after every test.
_APP_CONFIG_KEYS = (
    "maintenance_mode",
    "maintenance_message",
    "min_supported_app_version",
    "recommended_app_version",
    "store_url",
    "support_email",
    "support_whatsapp_url",
    "feature_flags",
    "announcements",
)


@pytest.fixture(autouse=True)
def _reset_singletons() -> Generator[None, None, None]:
    """Isolate the process-wide override/metrics/policy singletons."""
    plan_limit_overrides.reset()
    ai_metrics.reset()
    provider_policy.reset()
    yield
    plan_limit_overrides.reset()
    ai_metrics.reset()
    provider_policy.reset()


# ---------------------------------------------------------------------------
# Plan-limit overrides
# ---------------------------------------------------------------------------


async def test_plan_limit_override_applies_and_reverts() -> None:
    async with SessionLocal() as session:
        service = PlanLimitOverrideService(session, get_settings())
        values = service.parse({"daily_messages": "12345", "models": "default,strong"})
        await service.update("starter", values, admin_id=None)

        effective = get_plan_limits(get_settings(), PlanTier.STARTER)
        default = get_plan_limits(get_settings(), PlanTier.STARTER, apply_overrides=False)
        assert effective.daily_messages == 12345
        assert effective.models == ("default", "strong")
        # Fields left blank keep the compiled-in default.
        assert effective.daily_tokens == default.daily_tokens

        await service.clear("starter", admin_id=None)
        reverted = get_plan_limits(get_settings(), PlanTier.STARTER)
        assert reverted.daily_messages == default.daily_messages

        await session.execute(delete(PlanLimitOverride).where(PlanLimitOverride.plan == "starter"))
        await session.commit()


async def test_plan_override_validation_rejects_bad_feature() -> None:
    import fastapi

    with pytest.raises(fastapi.HTTPException):
        PlanLimitOverrideService.parse({"features": "ai_chat,nonsense"})


# ---------------------------------------------------------------------------
# App config: flags, payload, maintenance, caching
# ---------------------------------------------------------------------------


async def test_app_config_feature_flag_and_payload() -> None:
    async with SessionLocal() as session:
        service = AppConfigService(session, get_settings())
        config = await service.load()
        assert all(config["feature_flags"].values())

        await service.update(
            {"feature_flags": {"medications": False, "finance": True}, "maintenance_mode": False},
            admin_id=None,
        )
        reloaded = await service.load()
        assert reloaded["feature_flags"]["medications"] is False
        assert reloaded["feature_flags"]["voice"] is True

        payload = service.to_public(reloaded)
        assert set(payload) == {"maintenance", "versions", "features", "support", "announcements"}
        assert payload["features"]["medications"] is False
        # Support contact, editable from the admin "Configurações" page.
        assert payload["support"] == {
            "email": "suporte@novaiax.com.br",
            "whatsapp_url": None,
        }

        await session.execute(delete(AppConfig).where(AppConfig.key.in_(_APP_CONFIG_KEYS)))
        await session.commit()


async def test_support_contact_round_trip_and_validation() -> None:
    async with SessionLocal() as session:
        service = AppConfigService(session, get_settings())
        await service.update(
            {"support_email": "ajuda@exemplo.com.br", "support_whatsapp_url": "https://wa.me/5511999999999"},
            admin_id=None,
        )

        support = await service.load_support()
        assert support == {
            "email": "ajuda@exemplo.com.br",
            "whatsapp_url": "https://wa.me/5511999999999",
        }
        assert service.to_public(await service.load())["support"] == support

        with pytest.raises(ValueError, match="e-mail"):
            await service.update({"support_email": "nope"}, admin_id=None)
        with pytest.raises(ValueError, match="WhatsApp"):
            await service.update(
                {"support_whatsapp_url": "http://inseguro.example"}, admin_id=None
            )

        await session.execute(delete(AppConfig).where(AppConfig.key.in_(_APP_CONFIG_KEYS)))
        await session.commit()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


async def test_public_app_config_maintenance_and_cache(client: TestClient) -> None:
    async with SessionLocal() as session:
        service = AppConfigService(session, get_settings())
        await service.update({"maintenance_mode": True, "maintenance_message": "Voltamos logo"}, admin_id=None)

    first = client.get("/api/v1/app-config")
    assert first.status_code == 200
    body = first.json()
    assert body["maintenance"] == {"enabled": True, "message": "Voltamos logo"}
    etag = first.headers["etag"]

    # Revalidation with the matching ETag yields a bodyless 304.
    cached = client.get("/api/v1/app-config", headers={"If-None-Match": etag})
    assert cached.status_code == 304

    async with SessionLocal() as session:
        await session.execute(delete(AppConfig).where(AppConfig.key.in_(_APP_CONFIG_KEYS)))
        await session.commit()


# ---------------------------------------------------------------------------
# AI provider policy
# ---------------------------------------------------------------------------


async def test_suspended_account_message_carries_the_support_contact(client: TestClient) -> None:
    """The ``account_suspended`` 403 points at the configured support contact."""
    from datetime import UTC, datetime

    from app.models.user import User
    from sqlalchemy import update

    email = f"suspended-{uuid.uuid4().hex}@example.com"
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Conta Suspensa",
            "email": email,
            "password": "Password123",
            "birth_date": "1990-01-01",
        },
    )
    assert registered.status_code == 201, registered.text
    login = client.post("/api/v1/auth/login", json={"email": email, "password": "Password123"})
    assert login.status_code == 200, login.text

    async with SessionLocal() as session:
        await AppConfigService(session, get_settings()).update(
            {
                "support_email": "contato@exemplo.com.br",
                "support_whatsapp_url": "https://wa.me/5511999999999",
            },
            admin_id=None,
        )
        await session.execute(
            update(User).where(User.email == email).values(suspended_at=datetime.now(UTC))
        )
        await session.commit()

    login_after_suspension = client.post("/api/v1/auth/login", json={"email": email, "password": "Password123"})
    assert login_after_suspension.status_code == 200
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert refreshed.status_code == 200

    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login_after_suspension.json()['access_token']}"},
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "account_suspended"
    assert "contato@exemplo.com.br" in resp.text
    assert "https://wa.me/5511999999999" in resp.text

    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.execute(delete(AppConfig).where(AppConfig.key.in_(_APP_CONFIG_KEYS)))
        await session.commit()


async def test_provider_policy_applies_enable_and_priority() -> None:
    async with SessionLocal() as session:
        repo = AIProviderSettingRepository(session)
        await repo.upsert("groq", enabled=False, priority=5, updated_by=None)
        await session.commit()
        await refresh_provider_policy(session)

        assert provider_policy.is_enabled("groq", True) is False
        assert provider_policy.priority("groq", 10) == 5
        assert provider_policy.has_override("groq") is True

        await session.execute(delete(AIProviderSetting).where(AIProviderSetting.name == "groq"))
        await session.commit()
        await refresh_provider_policy(session)
        assert provider_policy.is_enabled("groq", True) is True


def test_ai_metrics_records_latency_and_errors() -> None:
    metrics = ai_metrics.provider("demo")
    metrics.record_success(120.0)
    metrics.record_success(80.0)
    metrics.record_failure("upstream_5xx")
    snapshot = ai_metrics.snapshot()["demo"]
    assert snapshot["successes"] == 2
    assert snapshot["failures"] == 1
    assert snapshot["latency_ms"]["p50"] == 100.0
    assert snapshot["recent_errors"][0]["reason"] == "upstream_5xx"


async def test_settings_console_renders_all_tabs(client: TestClient) -> None:
    import re

    from app.models.user import User, UserRole
    from sqlalchemy import update

    email = f"config-{uuid.uuid4().hex}@example.com"
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Config Admin",
            "email": email,
            "password": "Password123",
            "birth_date": "1990-01-01",
        },
    )
    assert registered.status_code == 201, registered.text
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(role=UserRole.ADMIN))
        await session.commit()

    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/admin/login").text)
    assert token is not None
    login = client.post(
        "/admin/login",
        data={"email": email, "password": "Password123", "csrf_token": token.group(1), "totp_code": ""},
        follow_redirects=False,
    )
    assert login.status_code == 303, login.text

    for tab in ("app", "limits", "ai", "contacts"):
        page = client.get(f"/admin/settings?tab={tab}")
        assert page.status_code == 200, page.text

    # The support contact is editable from the "Aplicativo" tab.
    app_tab = client.get("/admin/settings?tab=app")
    assert 'name="support_email"' in app_tab.text
    assert 'name="support_whatsapp_url"' in app_tab.text

    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.commit()
