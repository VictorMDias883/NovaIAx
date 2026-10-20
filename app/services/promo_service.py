"""
Public promotion service: redeem a promo code and list promotion cards.

Redemption is the one write in this module.  It runs inside a single
transaction that **locks the code row** (``SELECT ... FOR UPDATE``), checks the
global and per-user ceilings, inserts the grant row, and only then commits — so
two users racing for the last redemption of a ``max_redemptions=1`` code cannot
both win.  Every failure raises a :class:`PromoRedeemError` carrying a stable,
app-mappable code.

Nothing here changes a price or takes a payment: a code only records a
complimentary entitlement in :class:`~app.models.promo_redemption.PromoRedemption`
(which :class:`~app.services.subscription_service.SubscriptionService` then
folds into the effective plan) or tops up the caller's daily AI budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.clients.ai_quota import bonus_messages_key
from app.commands.redeem_promo_code_command import RedeemPromoCodeCommand
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.exceptions.promo_errors import PromoErrorCode, PromoRedeemError
from app.models.promo_code import PromoCode, PromoCodeKind
from app.models.promo_redemption import PromoRedemption
from app.models.promotion import Promotion, PromotionAudience
from app.repositories.promo_code_repository import PromoCodeRepository
from app.repositories.promo_redemption_repository import PromoRedemptionRepository
from app.repositories.promotion_repository import PromotionRepository
from app.repositories.user_agent_repository import UserAgentRepository
from app.schemas.promo_schemas import PromoRedeemResponse
from app.services.admin_audit_service import AdminAuditService
from app.services.subscription_service import SubscriptionService
from app.utils.datetime_utils import ensure_aware

logger = get_logger(__name__)


class PromoService:
    """Redeem promo codes and resolve the promotions a user should see."""

    def __init__(
        self,
        session: AsyncSession,
        redis_client: RedisClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.redis = redis_client
        self.settings = settings or get_settings()
        self.codes = PromoCodeRepository(session)
        self.redemptions = PromoRedemptionRepository(session)
        self.promotions = PromotionRepository(session)

    # -- Redemption ---------------------------------------------------------

    async def redeem(
        self, command: RedeemPromoCodeCommand, *, ip: str | None = None
    ) -> PromoRedeemResponse:
        """Redeem ``command.code`` for the caller, or raise ``PromoRedeemError``.

        ``ip`` is accepted for symmetry with the rate limiter (which runs in the
        router) and is not stored on the redemption row.
        """
        normalised = command.code.strip().lower()
        code = await self.codes.get_for_update(normalised)
        now = datetime.now(UTC)
        if code is None or not code.active:
            raise PromoRedeemError(PromoErrorCode.NOT_FOUND)
        if self._outside_window(code, now):
            raise PromoRedeemError(PromoErrorCode.EXPIRED)

        if code.max_redemptions is not None and code.max_redemptions >= 0:
            used = await self.redemptions.count_for_code(code.id)
            if used >= code.max_redemptions:
                raise PromoRedeemError(PromoErrorCode.EXHAUSTED)

        per_user = code.per_user_limit if code.per_user_limit > 0 else 1
        if await self.redemptions.count_for_user(code.id, command.user_id) >= per_user:
            raise PromoRedeemError(PromoErrorCode.ALREADY_REDEEMED)

        granted_until = self._grant_expiry(code, now)
        plan = self._grant_plan(code)
        bonus = code.bonus_messages if code.kind == PromoCodeKind.BONUS_QUOTA else None
        self.redemptions.add(
            PromoRedemption(
                code_id=code.id,
                user_id=command.user_id,
                source="promo",
                plan=plan,
                bonus_messages=bonus,
                granted_until=granted_until,
            )
        )
        # Locked read + insert + commit happen together; the lock is released on
        # commit, making the ceiling check atomic against concurrent redeemers.
        await self.session.commit()

        if bonus:
            await self._refresh_bonus(command.user_id, now)
        await self._audit_redeem(command.user_id, code, ip)
        logger.info(
            "promo_redeemed",
            extra={"user_id": str(command.user_id), "code_id": code.id, "kind": code.kind},
        )
        return PromoRedeemResponse(
            code=code.code,
            kind=code.kind,
            plan=plan,
            bonus_messages=bonus,
            expires_at=granted_until,
        )

    @staticmethod
    def _outside_window(code: PromoCode, now: datetime) -> bool:
        """True when ``now`` falls outside the code's validity window."""
        valid_from = ensure_aware(code.valid_from) if code.valid_from is not None else None
        valid_until = ensure_aware(code.valid_until) if code.valid_until is not None else None
        if valid_from is not None and now < valid_from:
            return True
        return bool(valid_until is not None and now > valid_until)

    @staticmethod
    def _grant_expiry(code: PromoCode, now: datetime) -> datetime | None:
        """End date of the grant (``None`` when the code has no duration)."""
        if code.duration_days and code.duration_days > 0:
            return now + timedelta(days=code.duration_days)
        return None

    @staticmethod
    def _grant_plan(code: PromoCode) -> str | None:
        """The plan a code grants, or ``None`` for a quota-only bonus."""
        if code.kind == PromoCodeKind.BONUS_QUOTA:
            return None
        if code.kind == PromoCodeKind.EXTEND_TRIAL:
            return code.plan or "trial"
        return code.plan

    async def _refresh_bonus(self, user_id: int, now: datetime) -> None:
        """Rewrite the user's aggregated bonus key to match the ledger."""
        if self.redis is None:
            return
        rows = await self.redemptions.list_for_user(user_id)
        active = [
            row
            for row in rows
            if row.bonus_messages
            and (row.granted_until is None or ensure_aware(row.granted_until) > now)
        ]
        total = sum(int(row.bonus_messages or 0) for row in active)
        key = bonus_messages_key(user_id)
        if total <= 0:
            await self.redis.delete(key)
            return
        expiries = [
            ensure_aware(row.granted_until) for row in active if row.granted_until is not None
        ]
        if len(expiries) < len(active):
            # At least one permanent bonus: the aggregate never expires.
            await self.redis.set(key, str(total))
            return
        latest = max(expiries)
        seconds = int((latest - now).total_seconds())
        if seconds > 0:
            await self.redis.set(key, str(total), ex=seconds)

    async def _audit_redeem(self, user_id: int, code: PromoCode, ip: str | None) -> None:
        """Record the redemption in the shared audit trail (no admin actor)."""
        await AdminAuditService(self.session).record(
            admin={"id": None, "email": f"user:{user_id}"},
            action="promo.redeem",
            entity="promo_code",
            entity_id=code.id,
            after={"kind": code.kind, "plan": code.plan},
            ip_address=ip,
        )

    # -- Promotions ---------------------------------------------------------

    async def list_active_promotions(self, user_id: int) -> list[Promotion]:
        """Return the promotion cards addressed to ``user_id``, best first."""
        now = datetime.now(UTC)
        candidates = await self.promotions.list_active(now)
        entitlement = await SubscriptionService(self.session, self.settings).resolve_entitlement(
            user_id
        )
        profile = await UserAgentRepository(self.session).get_by_user(user_id)
        group_slugs = set(profile.active_group_slugs) if profile is not None else set()
        plan_value = entitlement.plan.value
        return [
            promotion
            for promotion in candidates
            if self._matches_audience(promotion, plan_value, group_slugs)
        ]

    @staticmethod
    def _matches_audience(
        promotion: Promotion, plan_value: str, group_slugs: set[str]
    ) -> bool:
        """True when a promotion's audience includes the caller."""
        if promotion.audience == PromotionAudience.ALL.value:
            return True
        if promotion.audience == PromotionAudience.PLAN.value:
            plans = promotion.audience_plans or []
            return plan_value in plans
        if promotion.audience == PromotionAudience.GROUPS.value:
            groups = set(promotion.audience_groups or [])
            return bool(groups & group_slugs)
        return False


__all__ = ["PromoService"]
