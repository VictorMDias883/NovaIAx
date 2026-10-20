"""
Resolve, from the database, what a user is currently entitled to.

This is the one place that answers *"what plan is this user on right now?"*.
It reads the cached :class:`~app.models.subscription.Subscription` row, checks
whether it still entitles the user (``active``/``grace`` before expiry, or
``canceled`` before expiry), and otherwise falls back to the automatic
new-user trial and then the limited free tier.

The result is an :class:`Entitlement`: the effective :class:`PlanTier`, its
:class:`~app.clients.plan_limits.PlanLimits`, and enough context for
:func:`app.api.deps.require_plan` to choose between a 402 paywall and a 403
"renew your subscription".  Everything downstream — AI quota, group and
sub-agent ceilings, device commands — reads its numbers from here, never from
ad-hoc constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.clients.ai_types import PlanTier
from app.clients.plan_limits import (
    BASELINE_FEATURES,
    PLAN_RANK,
    PlanLimits,
    get_plan_limits,
    required_plan_for_feature,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.exceptions.plan_errors import PlanErrorCode, PlanRequiredError
from app.models.subscription import ENTITLED_STATUSES, Subscription, SubscriptionPlan, SubscriptionStatus
from app.repositories.promo_redemption_repository import PromoRedemptionRepository
from app.repositories.subscription_repository import SubscriptionRepository
from app.repositories.user_repository import UserRepository
from app.services.plan_limit_override_service import refresh_plan_limit_overrides
from app.utils.datetime_utils import ensure_aware

logger = get_logger(__name__)

#: Maps the persisted :class:`SubscriptionPlan` to the routing :class:`PlanTier`.
_PLAN_TIER: dict[SubscriptionPlan, PlanTier] = {
    SubscriptionPlan.STARTER: PlanTier.STARTER,
    SubscriptionPlan.PRO: PlanTier.PRO,
    SubscriptionPlan.PREMIUM: PlanTier.PREMIUM,
}


@dataclass(frozen=True)
class Entitlement:
    """What a user may do right now, and why.

    Attributes:
        plan: The effective :class:`PlanTier` (never the persisted paid plan
            for an unentitled user — those fall back to TRIAL/FREE).
        limits: The :class:`PlanLimits` for ``plan``.
        status: The stored subscription status, when a row exists
            (``None`` before the user's first purchase).
        is_trial: True when the effective access is the new-user trial.
        active: True when the effective plan's baseline features are usable
            (always True — the free tier keeps the app working).
        subscription_inactive: True when a subscription row exists but no
            longer entitles the user (on hold, paused, expired, revoked).
        has_subscription: True when a subscription row exists at all.
        product_id: The purchased Play product id, when known.
        expires_at: Entitlement end, when known.
        auto_renewing: Whether Play reports the subscription will renew.
        source: Where the effective plan comes from — ``play`` (a Play
            subscription), ``promo`` (a promo/manual grant), ``trial`` (the
            automatic new-user trial) or ``free``.
    """

    plan: PlanTier
    limits: PlanLimits
    status: SubscriptionStatus | None
    is_trial: bool
    active: bool
    subscription_inactive: bool
    has_subscription: bool
    product_id: str | None
    expires_at: datetime | None
    auto_renewing: bool
    source: str = "free"

    @property
    def plan_tier(self) -> PlanTier:
        """Alias for :attr:`plan` (the name the AI layer threads through)."""
        return self.plan


def _aware(value: datetime | None) -> datetime | None:
    """Return ``value`` as a timezone-aware UTC datetime (SQLite yields naive)."""
    if value is None:
        return None
    return ensure_aware(value)


def _promo_plan_tier(plan: str | None) -> PlanTier | None:
    """Map a granted plan string to a :class:`PlanTier` (``None`` if unknown)."""
    if not plan:
        return None
    try:
        return PlanTier(plan)
    except ValueError:
        return None


def _expires_later(candidate: datetime | None, current: datetime | None) -> bool:
    """True when ``candidate`` outlasts ``current`` (``None`` = permanent)."""
    if candidate is None:
        return True
    if current is None:
        return False
    return candidate > current


def _is_entitled(subscription: Subscription, now: datetime) -> bool:
    """True when ``subscription`` still grants paid features at ``now``."""
    expires_at = _aware(subscription.expires_at)
    if subscription.status in ENTITLED_STATUSES:
        return expires_at is None or expires_at > now
    if subscription.status is SubscriptionStatus.CANCELED:
        return expires_at is not None and expires_at > now
    return False


def enforce_feature(entitlement: Entitlement, feature: str) -> None:
    """Raise a :class:`PlanRequiredError` when ``feature`` is not available.

    A user with an inactive subscription gets a 403 (renew) for anything
    beyond the free baseline; a user whose plan simply lacks the feature gets
    a 402 paywall with the cheapest plan that would unlock it.
    """
    if entitlement.subscription_inactive and feature not in BASELINE_FEATURES:
        raise PlanRequiredError(
            PlanErrorCode.SUBSCRIPTION_INACTIVE,
            feature=feature,
            current_plan=entitlement.plan.value,
            required_plan=(required_plan_for_feature(feature) or PlanTier.STARTER).value,
        )
    if not entitlement.limits.includes(feature):
        raise PlanRequiredError(
            PlanErrorCode.UPGRADE_REQUIRED,
            feature=feature,
            current_plan=entitlement.plan.value,
            required_plan=(required_plan_for_feature(feature) or PlanTier.STARTER).value,
        )


def enforce_count_limit(
    entitlement: Entitlement,
    *,
    feature: str,
    count: int,
    limit: int,
    label: str,
) -> None:
    """Raise a 402 when a countable ceiling (``limit``) is exceeded.

    ``limit`` of ``0`` or less means unlimited.
    """
    if limit >= 0 and count > limit:
        raise PlanRequiredError(
            PlanErrorCode.LIMIT_REACHED,
            feature=feature,
            current_plan=entitlement.plan.value,
            required_plan=(required_plan_for_feature(feature) or PlanTier.STARTER).value,
            limit=limit,
            detail=(
                f"Você atingiu o limite de {label} do seu plano "
                f"({limit}). Faça upgrade para ampliar."
            ),
        )


class SubscriptionService:
    """Resolves plan entitlement and exposes the live subscription row."""

    def __init__(self, session, settings: Settings | None = None) -> None:  # noqa: ANN001 - AsyncSession
        self.session = session
        self.settings = settings or get_settings()
        self.repository = SubscriptionRepository(session)

    async def get_subscription(self, user_id: int) -> Subscription | None:
        """Return the user's stored subscription row, if any."""
        return await self.repository.get_by_user(user_id)

    async def resolve_entitlement(self, user_id: int) -> Entitlement:
        """Return the effective :class:`Entitlement` for ``user_id``.

        The effective plan is the **highest** of the active Google Play
        subscription and any still-active promo/manual grant.  Play RTDN events
        never remove a grant, so a promo that outranks the subscription keeps
        winning until it expires — at which point the resolver automatically
        falls back to the subscription (or the trial/free tier).
        """
        subscription = await self.repository.get_by_user(user_id)
        now = datetime.now(UTC)
        # Keep the process-wide ceiling-override snapshot fresh (cheap no-op
        # until its TTL elapses) so a panel change applies without a redeploy.
        await refresh_plan_limit_overrides(self.session, self.settings)
        play_entitled = subscription is not None and _is_entitled(subscription, now)
        promo_plan, promo_expires = await self._active_promo_plan(user_id, now)

        play_rank = PLAN_RANK.get(subscription.plan.value, -1) if play_entitled and subscription else -1
        promo_rank = PLAN_RANK.get(promo_plan.value, -1) if promo_plan is not None else -1

        if promo_plan is not None and promo_rank > play_rank:
            return self._promo_entitlement(subscription, promo_plan, promo_expires)

        if play_entitled and subscription is not None:
            plan = _PLAN_TIER[subscription.plan]
            return Entitlement(
                plan=plan,
                limits=get_plan_limits(self.settings, plan),
                status=subscription.status,
                is_trial=subscription.is_trial,
                active=True,
                subscription_inactive=False,
                has_subscription=True,
                product_id=subscription.product_id,
                expires_at=_aware(subscription.expires_at),
                auto_renewing=subscription.auto_renewing,
                source="play",
            )

        plan = await self._fallback_plan(user_id, now)
        return Entitlement(
            plan=plan,
            limits=get_plan_limits(self.settings, plan),
            status=subscription.status if subscription is not None else None,
            is_trial=plan is PlanTier.TRIAL,
            active=True,
            subscription_inactive=subscription is not None,
            has_subscription=subscription is not None,
            product_id=subscription.product_id if subscription is not None else None,
            expires_at=_aware(subscription.expires_at) if subscription is not None else None,
            auto_renewing=subscription.auto_renewing if subscription is not None else False,
            source="trial" if plan is PlanTier.TRIAL else "free",
        )

    async def _active_promo_plan(
        self, user_id: int, now: datetime
    ) -> tuple[PlanTier | None, datetime | None]:
        """Return the highest still-active granted plan and its expiry."""
        grants = await PromoRedemptionRepository(self.session).active_plan_grants(user_id, now)
        best_plan: PlanTier | None = None
        best_rank = -1
        best_expiry: datetime | None = None
        for grant in grants:
            tier = _promo_plan_tier(grant.plan)
            if tier is None:
                continue
            rank = PLAN_RANK.get(tier.value, -1)
            expiry = _aware(grant.granted_until)
            if rank > best_rank or (rank == best_rank and _expires_later(expiry, best_expiry)):
                best_plan, best_rank, best_expiry = tier, rank, expiry
        return best_plan, best_expiry

    def _promo_entitlement(
        self,
        subscription: Subscription | None,
        plan: PlanTier,
        expires_at: datetime | None,
    ) -> Entitlement:
        """Build the entitlement for a promo/manual grant that wins.

        ``subscription_inactive`` is forced to ``False``: an active grant is a
        valid, non-renewal entitlement, so it must not trip the "renew your
        subscription" gate on features above the free baseline.
        """
        return Entitlement(
            plan=plan,
            limits=get_plan_limits(self.settings, plan),
            status=subscription.status if subscription is not None else None,
            is_trial=plan is PlanTier.TRIAL,
            active=True,
            subscription_inactive=False,
            has_subscription=subscription is not None,
            product_id=subscription.product_id if subscription is not None else None,
            expires_at=expires_at,
            auto_renewing=subscription.auto_renewing if subscription is not None else False,
            source="promo",
        )

    async def _fallback_plan(self, user_id: int, now: datetime) -> PlanTier:
        """Return TRIAL while the account is new, otherwise FREE."""
        user = await UserRepository(self.session).get_by_id(user_id)
        if user is None:
            return PlanTier.FREE
        created_at = _aware(user.created_at)
        if created_at is None:
            return PlanTier.FREE
        trial_days = max(self.settings.billing_free_trial_days, 0)
        if now < created_at + timedelta(days=trial_days):
            return PlanTier.TRIAL
        return PlanTier.FREE
