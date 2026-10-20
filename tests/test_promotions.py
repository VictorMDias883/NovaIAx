"""
Tests for promo-code redemption and the promotions surface.

Scope (logic + critical flows only):

* the redemption ceilings — global ``max_redemptions`` and per-user limit —
  and the stable error codes for not-found / expired;
* how a grant folds into the effective plan and falls back on expiry, and how
  it outranks (and survives) a Google Play subscription;
* the bonus-quota top-up reaching :class:`~app.clients.ai_quota.AIQuotaTracker`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from app.cache.redis_client import RedisClient
from app.clients.ai_quota import AIQuotaTracker
from app.clients.ai_types import PlanTier
from app.commands.redeem_promo_code_command import RedeemPromoCodeCommand
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.exceptions.promo_errors import PromoErrorCode, PromoRedeemError
from app.main import app
from app.models.promo_code import PromoCode, PromoCodeKind
from app.models.promo_redemption import PromoRedemption
from app.models.promotion import Promotion
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.user import User, UserRole
from app.services.promo_service import PromoService
from app.services.subscription_service import SubscriptionService
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

_PASSWORD = "Password123"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _email() -> str:
    return f"promo-{uuid.uuid4().hex}@example.com"


async def _make_user(session) -> User:  # noqa: ANN001 - AsyncSession
    user = User(full_name="Promo User", email=_email(), password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _code(session, **kwargs) -> PromoCode:  # noqa: ANN001 - AsyncSession
    defaults = {"kind": PromoCodeKind.GRANT_PLAN, "plan": "pro", "duration_days": 30}
    code = PromoCode(code=uuid.uuid4().hex[:12], **{**defaults, **kwargs})
    session.add(code)
    return code


# ---------------------------------------------------------------------------
# Redemption ceilings and stable errors
# ---------------------------------------------------------------------------


async def test_redeem_enforces_global_ceiling() -> None:
    async with SessionLocal() as session:
        code = _code(session, max_redemptions=1)
        first = await _make_user(session)
        second = await _make_user(session)
        await session.commit()
        service = PromoService(session, RedisClient(get_settings()))

        await service.redeem(RedeemPromoCodeCommand(code=code.code, user_id=first.id))

        with pytest.raises(PromoRedeemError) as exc:
            await service.redeem(RedeemPromoCodeCommand(code=code.code, user_id=second.id))
        assert exc.value.code is PromoErrorCode.EXHAUSTED
        assert exc.value.status_code == 409

        await session.execute(delete(User).where(User.id.in_([first.id, second.id])))
        await session.execute(delete(PromoRedemption).where(PromoRedemption.code_id == code.id))
        await session.execute(delete(PromoCode).where(PromoCode.id == code.id))
        await session.commit()


async def test_redeem_enforces_per_user_limit() -> None:
    async with SessionLocal() as session:
        code = _code(session)
        user = await _make_user(session)
        await session.commit()
        service = PromoService(session, RedisClient(get_settings()))

        await service.redeem(RedeemPromoCodeCommand(code=code.code, user_id=user.id))
        with pytest.raises(PromoRedeemError) as exc:
            await service.redeem(RedeemPromoCodeCommand(code=code.code, user_id=user.id))
        assert exc.value.code is PromoErrorCode.ALREADY_REDEEMED

        await session.execute(delete(User).where(User.id == user.id))
        await session.execute(delete(PromoRedemption).where(PromoRedemption.code_id == code.id))
        await session.execute(delete(PromoCode).where(PromoCode.id == code.id))
        await session.commit()


async def test_redeem_rejects_unknown_and_expired() -> None:
    async with SessionLocal() as session:
        user = await _make_user(session)
        expired = _code(session, valid_until=datetime.now(UTC) - timedelta(days=1))
        await session.commit()
        service = PromoService(session, RedisClient(get_settings()))

        with pytest.raises(PromoRedeemError) as missing:
            await service.redeem(RedeemPromoCodeCommand(code="does-not-exist", user_id=user.id))
        assert missing.value.status_code == 404

        with pytest.raises(PromoRedeemError) as stale:
            await service.redeem(RedeemPromoCodeCommand(code=expired.code, user_id=user.id))
        assert stale.value.code is PromoErrorCode.EXPIRED
        assert stale.value.status_code == 410

        await session.execute(delete(User).where(User.id == user.id))
        await session.execute(delete(PromoCode).where(PromoCode.id == expired.id))
        await session.commit()


# ---------------------------------------------------------------------------
# Plan grant precedence and fallback
# ---------------------------------------------------------------------------


async def test_promo_outranks_play_and_falls_back_on_expiry() -> None:
    async with SessionLocal() as session:
        user = await _make_user(session)
        session.add(
            Subscription(
                user_id=user.id,
                plan=SubscriptionPlan.STARTER,
                status=SubscriptionStatus.ACTIVE,
                product_id="starter_monthly",
                purchase_token_hash="a" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=30),
                auto_renewing=True,
            )
        )
        await session.commit()

        service = SubscriptionService(session, get_settings())
        play = await service.resolve_entitlement(user.id)
        assert play.plan is PlanTier.STARTER
        assert play.source == "play"

        # A higher promotional grant wins even while the Play plan is active.
        session.add(
            PromoRedemption(
                user_id=user.id,
                source="promo",
                plan="premium",
                granted_until=datetime.now(UTC) + timedelta(days=10),
            )
        )
        await session.commit()
        promoted = await service.resolve_entitlement(user.id)
        assert promoted.plan is PlanTier.PREMIUM
        assert promoted.source == "promo"
        # A promo entitlement must never trip the "renew" gate.
        assert promoted.subscription_inactive is False

        # Expiring the grant falls back to the still-active Play plan.
        await session.execute(
            update(PromoRedemption)
            .where(PromoRedemption.user_id == user.id)
            .values(granted_until=datetime.now(UTC) - timedelta(days=1))
        )
        await session.commit()
        fallen_back = await service.resolve_entitlement(user.id)
        assert fallen_back.plan is PlanTier.STARTER
        assert fallen_back.source == "play"

        await session.execute(
            delete(PromoRedemption).where(PromoRedemption.user_id == user.id)
        )
        await session.execute(delete(Subscription).where(Subscription.user_id == user.id))
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


async def test_tie_defers_to_play() -> None:
    async with SessionLocal() as session:
        user = await _make_user(session)
        session.add(
            Subscription(
                user_id=user.id,
                plan=SubscriptionPlan.PRO,
                status=SubscriptionStatus.ACTIVE,
                product_id="pro_monthly",
                purchase_token_hash="b" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=30),
                auto_renewing=True,
            )
        )
        session.add(
            PromoRedemption(
                user_id=user.id,
                source="promo",
                plan="pro",
                granted_until=datetime.now(UTC) + timedelta(days=5),
            )
        )
        await session.commit()

        entitlement = await SubscriptionService(session, get_settings()).resolve_entitlement(
            user.id
        )
        assert entitlement.plan is PlanTier.PRO
        assert entitlement.source == "play"

        await session.execute(
            delete(PromoRedemption).where(PromoRedemption.user_id == user.id)
        )
        await session.execute(delete(Subscription).where(Subscription.user_id == user.id))
        await session.execute(delete(User).where(User.id == user.id))
        await session.commit()


# ---------------------------------------------------------------------------
# Bonus quota
# ---------------------------------------------------------------------------


async def test_bonus_code_raises_daily_message_budget() -> None:
    async with SessionLocal() as session:
        code = _code(
            session,
            kind=PromoCodeKind.BONUS_QUOTA,
            plan=None,
            duration_days=None,
            bonus_messages=250,
        )
        user = await _make_user(session)
        await session.commit()

        redis = RedisClient(get_settings())
        service = PromoService(session, redis, get_settings())
        await service.redeem(RedeemPromoCodeCommand(code=code.code, user_id=user.id))

        tracker = AIQuotaTracker(redis, get_settings())
        boosted = await tracker.effective_limits(user.id, PlanTier.FREE)
        other = await _make_user(session)
        unboosted = await tracker.effective_limits(other.id, PlanTier.FREE)
        assert boosted.daily_messages == unboosted.daily_messages + 250

        await session.execute(delete(User).where(User.id.in_([user.id, other.id])))
        await session.execute(delete(PromoRedemption).where(PromoRedemption.code_id == code.id))
        await session.execute(delete(PromoCode).where(PromoCode.id == code.id))
        await session.commit()


# ---------------------------------------------------------------------------
# Admin console rendering (guards against missing templates / context keys)
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


def _csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "csrf token not found in page"
    return match.group(1)


async def _set_role(email: str, role: UserRole) -> None:
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()


def _login(client: TestClient, email: str) -> None:
    token = _csrf_from(client.get("/admin/login").text)
    response = client.post(
        "/admin/login",
        data={"email": email, "password": _PASSWORD, "csrf_token": token, "totp_code": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


async def _latest_id(model) -> int:  # noqa: ANN001
    async with SessionLocal() as session:
        result = await session.execute(select(model.id).order_by(model.id.desc()))
        value = result.scalars().first()
        assert value is not None
        return int(value)


async def test_promotions_console_renders_and_creates() -> None:
    email = _email()
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "full_name": "Promo Admin",
                "email": email,
                "password": _PASSWORD,
                "birth_date": "1990-01-01",
            },
        )
        assert registered.status_code == 201, registered.text
        await _set_role(email, UserRole.ADMIN)
        _login(client, email)
        token = _csrf_from(client.get("/admin/promotions").text)

        assert client.get("/admin/promotions?tab=codes").status_code == 200
        assert client.get("/admin/promotions?tab=promotions").status_code == 200
        assert client.get("/admin/promotions/new").status_code == 200
        assert client.get("/admin/promotions/grants").status_code == 200

        code_value = uuid.uuid4().hex[:10]
        created = client.post(
            "/admin/promotions/codes",
            data={
                "csrf_token": token,
                "code": code_value,
                "kind": "grant_plan",
                "plan": "pro",
                "duration_days": "30",
                "per_user_limit": "1",
                "active": "true",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303, created.text
        detail = client.get(f"/admin/promotions/codes/{await _latest_id(PromoCode)}")
        assert detail.status_code == 200

        promotion = client.post(
            "/admin/promotions",
            data={
                "csrf_token": token,
                "title": "Oferta de teste",
                "message": "Teste",
                "cta_target": "plans",
                "audience": "all",
                "priority": "1",
                "active": "true",
            },
            follow_redirects=False,
        )
        assert promotion.status_code == 303, promotion.text
        edit = client.get(f"/admin/promotions/{await _latest_id(Promotion)}/edit")
        assert edit.status_code == 200

    async with SessionLocal() as session:
        await session.execute(delete(Promotion).where(Promotion.title == "Oferta de teste"))
        await session.execute(delete(PromoCode).where(PromoCode.code == code_value))
        await session.execute(delete(User).where(User.email == email))
        await session.commit()


