"""Tests for the Google Play billing configuration and RTDN authentication.

Covers:

1. The Play application-id default is the real one (``br.com.novaiax.agente``).
2. Production boot requires a syntactically valid package name.
3. The Pub/Sub push endpoint (``/api/v1/billing/google/rtdn``) only accepts a
   request carrying the configured shared secret, is closed when nothing is
   configured, and rejects a wrong secret.
"""

import asyncio
import time

import jwt
import pytest
from app.api.v1 import subscription_router
from app.core.config import Settings, _validate_production_secrets, get_settings
from app.exceptions.plan_errors import PlanRequiredError
from fastapi import HTTPException
from starlette.requests import Request


def _prod_settings(**overrides) -> Settings:
    base = {
        "environment": "production",
        "secret_key": "an-obviously-strong-secret-value",
        "master_api_key": "a-strong-non-placeholder-master-key",
        "groq_api_key": "gsk_example",
        "database_url": "postgresql+asyncpg://user:pass@db:5432/novaiax",
        "redis_url": "rediss://default:pass@endpoint:6379",
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# 1-2. Package-name default and production validation
# ---------------------------------------------------------------------------


def test_play_package_default_is_the_real_application_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_PLAY_PACKAGE_NAME", raising=False)
    assert Settings().google_play_package_name == "br.com.novaiax.agente"


def test_production_accepts_a_valid_package_name(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prod_settings(google_play_package_name="br.com.novaiax.agente")
    monkeypatch.setattr("app.core.config._settings", settings, raising=False)
    # Should not raise.
    _validate_production_secrets()


def test_production_rejects_a_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prod_settings(secret_key="too-short")
    monkeypatch.setattr("app.core.config._settings", settings, raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY must contain at least 32 bytes"):
        _validate_production_secrets()


def test_production_rejects_a_short_master_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prod_settings(master_api_key="too-short")
    monkeypatch.setattr("app.core.config._settings", settings, raising=False)
    with pytest.raises(RuntimeError, match="MASTER_API_KEY must contain at least 32 bytes"):
        _validate_production_secrets()


def test_production_rejects_an_age_gate_below_18(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prod_settings(minimum_account_age_years=17)
    monkeypatch.setattr("app.core.config._settings", settings, raising=False)
    with pytest.raises(RuntimeError, match="MINIMUM_ACCOUNT_AGE_YEARS"):
        _validate_production_secrets()


@pytest.mark.parametrize("bad", ["", "not a package", "com.novaiax.", ".com.novaiax"])
def test_production_rejects_an_invalid_package_name(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    settings = _prod_settings(google_play_package_name=bad)
    monkeypatch.setattr("app.core.config._settings", settings, raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_PLAY_PACKAGE_NAME"):
        _validate_production_secrets()


# ---------------------------------------------------------------------------
# 3. RTDN authentication
# ---------------------------------------------------------------------------


def _request(query: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/billing/google/rtdn",
        "query_string": query,
        "headers": headers or [],
    }
    return Request(scope)


def test_rtdn_accepts_the_shared_secret_via_query(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_play_rtdn_token", "s3cret-token")
    monkeypatch.setattr(settings, "google_play_rtdn_audience", None)

    # Returns None (authenticated) instead of raising.
    assert (
        asyncio.run(subscription_router._authorize_rtdn(_request(query=b"token=s3cret-token"))) is None
    )


def test_rtdn_accepts_the_shared_secret_via_header(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_play_rtdn_token", "s3cret-token")
    monkeypatch.setattr(settings, "google_play_rtdn_audience", None)

    request = _request(headers=[(b"x-rtdn-token", b"s3cret-token")])
    assert asyncio.run(subscription_router._authorize_rtdn(request)) is None


def test_rtdn_rejects_a_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_play_rtdn_token", "s3cret-token")
    monkeypatch.setattr(settings, "google_play_rtdn_audience", None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(subscription_router._authorize_rtdn(_request(query=b"token=wrong")))
    assert exc_info.value.status_code == 401


def test_rtdn_is_closed_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_play_rtdn_token", None)
    monkeypatch.setattr(settings, "google_play_rtdn_audience", None)
    monkeypatch.setattr(subscription_router, "get_settings", lambda: settings)

    with pytest.raises(PlanRequiredError):
        asyncio.run(subscription_router._authorize_rtdn(_request()))


def test_oidc_verification_selects_matching_jwks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(subscription_router, "_jwks_cache", (time.time(), {"keys": [jwk]}))
    token = jwt.encode(
        {"iss": "https://accounts.google.com", "aud": "novaiax-rtdn", "sub": "test", "exp": int(time.time()) + 60},
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    assert asyncio.run(subscription_router._verify_oidc_token(token, "novaiax-rtdn"))
    assert not asyncio.run(subscription_router._verify_oidc_token(token, "wrong-audience"))
