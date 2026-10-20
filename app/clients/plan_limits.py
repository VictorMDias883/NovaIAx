"""Single source of truth for every subscription plan ceiling.

The whole product is gated from here: the AI daily quota
(:mod:`app.clients.ai_quota`), the number of audience groups and sub-agents a
user may activate, the daily device-command budget and the set of AI model
tiers a plan may reach.  Keeping every number in :data:`PLAN_LIMITS` means a
product change (a bigger quota, a new feature gate) is a one-line edit in one
place instead of a hunt through services.

Plans
-----
``FREE``
    The always-available, deliberately limited tier.  The daily message/token
    budget is taken from the operational ``AI_DEFAULT_DAILY_*`` settings so it
    stays tunable without a deploy.
``TRIAL``
    Granted automatically to new users for ``BILLING_FREE_TRIAL_DAYS`` so the
    app is fully usable before the first purchase.  It behaves like a small
    paid plan (groups + sub-agents + device commands).
``STARTER`` / ``PRO`` / ``PREMIUM``
    The paid plans sold through Google Play (R$ 21,90 / R$ 26,80 / R$ 32,70
    per month).

A ceiling of ``<= 0`` means *unlimited*.  Feature gating is expressed as a
set of :data:`FEATURE_*` flags: ``require_plan(feature)`` (see
:func:`app.api.deps.require_plan`) checks membership.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.clients.ai_types import PlanTier
from app.clients.plan_limits_overrides import plan_limit_overrides
from app.core.config import Settings

# ---------------------------------------------------------------------------
# Feature flags — the vocabulary the product gates on.
# ---------------------------------------------------------------------------
FEATURE_AI_CHAT = "ai_chat"
FEATURE_REMINDERS = "reminders"
FEATURE_GROUPS = "groups"
FEATURE_SUB_AGENTS = "sub_agents"
FEATURE_DEVICE_ACTIONS = "device_actions"
FEATURE_CALENDAR = "calendar"
FEATURE_AUTOMATION = "automation"
FEATURE_ADVANCED_AUTOMATION = "advanced_automation"
FEATURE_PRIORITY_SUPPORT = "priority_support"
FEATURE_STRONG_MODEL = "strong_model"

#: Features every account can use even with no purchase and an expired trial.
BASELINE_FEATURES: frozenset[str] = frozenset({FEATURE_AI_CHAT, FEATURE_REMINDERS})

#: Cheapest plan that unlocks each gated feature (``None`` = available free).
_FEATURE_REQUIRED_PLAN: dict[str, PlanTier] = {
    FEATURE_AI_CHAT: PlanTier.FREE,
    FEATURE_REMINDERS: PlanTier.FREE,
    FEATURE_GROUPS: PlanTier.STARTER,
    FEATURE_SUB_AGENTS: PlanTier.STARTER,
    FEATURE_DEVICE_ACTIONS: PlanTier.STARTER,
    FEATURE_CALENDAR: PlanTier.PRO,
    FEATURE_AUTOMATION: PlanTier.PRO,
    FEATURE_STRONG_MODEL: PlanTier.PREMIUM,
    FEATURE_ADVANCED_AUTOMATION: PlanTier.PREMIUM,
    FEATURE_PRIORITY_SUPPORT: PlanTier.PREMIUM,
}

#: Rank used to compare a plan against a sub-agent's ``min_plan``.
PLAN_RANK: dict[str, int] = {
    PlanTier.FREE.value: 0,
    PlanTier.TRIAL.value: 1,
    PlanTier.STARTER.value: 1,
    PlanTier.PRO.value: 2,
    PlanTier.PREMIUM.value: 3,
}


@dataclass(frozen=True)
class PlanLimits:
    """Every ceiling and feature flag that applies to one plan.

    Attributes:
        plan: The :class:`PlanTier` this configuration describes.
        display_name: PT-BR label shown in the app.
        price_brl: PT-BR price string (``None`` for the free/trial tiers).
        max_groups: Maximum simultaneously active audience groups
            (``<= 0`` = unlimited).
        max_sub_agents: Maximum simultaneously active sub-agents.
        daily_messages: AI messages per UTC day (``<= 0`` = unlimited).
        daily_tokens: Estimated AI tokens per UTC day (``<= 0`` = unlimited).
        daily_device_actions: Device-command agent calls per UTC day.
        models: Ordered AI model-selection keys the plan may reach.
        features: Set of :data:`FEATURE_*` flags included in the plan.
    """

    plan: PlanTier
    display_name: str
    price_brl: str | None
    max_groups: int
    max_sub_agents: int
    daily_messages: int
    daily_tokens: int
    daily_device_actions: int
    models: tuple[str, ...]
    features: frozenset[str] = field(default_factory=frozenset)

    @property
    def unlimited(self) -> bool:
        """True when neither the message nor the token budget is enforced."""
        return self.daily_messages <= 0 and self.daily_tokens <= 0

    def allows(self, *, messages: int, tokens: int) -> bool:
        """True when already-consumed counts fit inside the daily budget."""
        if self.unlimited:
            return True
        within_messages = self.daily_messages <= 0 or messages < self.daily_messages
        within_tokens = self.daily_tokens <= 0 or tokens < self.daily_tokens
        return within_messages and within_tokens

    def includes(self, feature: str) -> bool:
        """True when the plan exposes ``feature``."""
        return feature in self.features


_ALL_FEATURES = frozenset(
    {
        FEATURE_AI_CHAT,
        FEATURE_REMINDERS,
        FEATURE_GROUPS,
        FEATURE_SUB_AGENTS,
        FEATURE_DEVICE_ACTIONS,
        FEATURE_CALENDAR,
        FEATURE_AUTOMATION,
        FEATURE_ADVANCED_AUTOMATION,
        FEATURE_PRIORITY_SUPPORT,
        FEATURE_STRONG_MODEL,
    }
)

#: Public alias of every :data:`FEATURE_*` flag (admin validation uses it).
ALL_FEATURES = _ALL_FEATURES

#: The canonical per-plan configuration.  This is the single ``PlanLimits``
#: config the whole application reads.
PLAN_LIMITS: dict[PlanTier, PlanLimits] = {
    PlanTier.FREE: PlanLimits(
        plan=PlanTier.FREE,
        display_name="Gratuito",
        price_brl=None,
        max_groups=0,
        max_sub_agents=0,
        daily_messages=200,
        daily_tokens=50_000,
        daily_device_actions=10,
        models=("default",),
        features=frozenset({FEATURE_AI_CHAT, FEATURE_REMINDERS}),
    ),
    PlanTier.TRIAL: PlanLimits(
        plan=PlanTier.TRIAL,
        display_name="Teste grátis",
        price_brl=None,
        max_groups=3,
        max_sub_agents=2,
        daily_messages=300,
        daily_tokens=80_000,
        daily_device_actions=50,
        models=("default",),
        features=frozenset(
            {
                FEATURE_AI_CHAT,
                FEATURE_REMINDERS,
                FEATURE_GROUPS,
                FEATURE_SUB_AGENTS,
                FEATURE_DEVICE_ACTIONS,
            }
        ),
    ),
    PlanTier.STARTER: PlanLimits(
        plan=PlanTier.STARTER,
        display_name="Starter",
        price_brl="R$ 21,90/mês",
        max_groups=1,
        max_sub_agents=1,
        daily_messages=300,
        daily_tokens=80_000,
        daily_device_actions=50,
        models=("default",),
        features=frozenset(
            {
                FEATURE_AI_CHAT,
                FEATURE_REMINDERS,
                FEATURE_GROUPS,
                FEATURE_SUB_AGENTS,
                FEATURE_DEVICE_ACTIONS,
            }
        ),
    ),
    PlanTier.PRO: PlanLimits(
        plan=PlanTier.PRO,
        display_name="Pro",
        price_brl="R$ 26,80/mês",
        max_groups=3,
        max_sub_agents=2,
        daily_messages=800,
        daily_tokens=200_000,
        daily_device_actions=150,
        models=("default", "strong"),
        features=frozenset(
            {
                FEATURE_AI_CHAT,
                FEATURE_REMINDERS,
                FEATURE_GROUPS,
                FEATURE_SUB_AGENTS,
                FEATURE_DEVICE_ACTIONS,
                FEATURE_CALENDAR,
                FEATURE_AUTOMATION,
                FEATURE_STRONG_MODEL,
            }
        ),
    ),
    PlanTier.PREMIUM: PlanLimits(
        plan=PlanTier.PREMIUM,
        display_name="Premium",
        price_brl="R$ 32,70/mês",
        max_groups=-1,
        max_sub_agents=-1,
        daily_messages=2_000,
        daily_tokens=600_000,
        daily_device_actions=500,
        models=("default", "strong", "strongest"),
        features=_ALL_FEATURES,
    ),
}


def get_plan_limits(
    settings: Settings | None,
    plan_tier: PlanTier | None = None,
    *,
    apply_overrides: bool = True,
) -> PlanLimits:
    """Return the :class:`PlanLimits` for ``plan_tier`` (defaults to ``FREE``).

    The ``FREE`` tier's daily budget is read from the operational
    ``AI_DEFAULT_DAILY_MESSAGES`` / ``AI_DEFAULT_DAILY_TOKENS`` settings so it
    stays configurable from the environment; every other ceiling is static.

    Any operator override loaded from ``plan_limit_overrides`` (see
    :mod:`app.clients.plan_limits_overrides`) is folded in last, so a change
    made in the panel applies without a redeploy.  Pass
    ``apply_overrides=False`` to get the compiled-in default (the admin panel
    uses this to show the default next to the override).
    """
    key = plan_tier if isinstance(plan_tier, PlanTier) else PlanTier.FREE
    limits = PLAN_LIMITS.get(key, PLAN_LIMITS[PlanTier.FREE])
    if key is PlanTier.FREE and settings is not None:
        limits = replace(
            limits,
            daily_messages=settings.ai_default_daily_messages,
            daily_tokens=settings.ai_default_daily_tokens,
        )
    if not apply_overrides:
        return limits
    return plan_limit_overrides.apply(limits)


def required_plan_for_feature(feature: str) -> PlanTier | None:
    """Return the cheapest plan that unlocks ``feature`` (``None`` if free)."""
    plan = _FEATURE_REQUIRED_PLAN.get(feature)
    if plan is PlanTier.FREE:
        return None
    return plan
