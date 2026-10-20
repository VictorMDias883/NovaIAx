"""Per-user daily AI quota tracking.

Roughly "monetize the AI": each user has a daily budget (messages and tokens)
that resets at UTC midnight.  The per-tier ceilings come from
:mod:`app.clients.plan_limits` (re-exported here as :func:`get_plan_limits`),
the single ``PlanLimits`` configuration the whole app reads.

Counters live in Redis (or the in-memory fallback) under
``ai_quota:messages:<user_id>:<YYYY-MM-DD>``,
``ai_quota:tokens:<user_id>:<YYYY-MM-DD>`` and
``ai_quota:device_actions:<user_id>:<YYYY-MM-DD>``.  The window is keyed by
the UTC day, so the reset is implicit: a new day is just a new key.  The TTL is
set on every increment so keys are eventually garbage-collected even for users
that never come back.

Quota is checked *before* a request is served (fail fast with a 429 the app
can show) and usage is recorded *after* the provider answers, so a failed
request does not burn today's budget.  Anonymous/system requests (``user_id``
is ``None``) skip the quota entirely.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.cache.redis_client import RedisClient
from app.clients.ai_types import PlanTier
from app.clients.plan_limits import PlanLimits, get_plan_limits  # noqa: F401 - re-exported
from app.core.config import Settings
from app.core.logging import get_logger
from app.exceptions.ai_errors import AIErrorCode, AIUpstreamError
from app.utils.coercion import coerce_int

logger = get_logger(__name__)

#: Redis key holding a user's aggregate active promo bonus (extra daily
#: messages).  Written when a ``bonus_quota`` code is redeemed and read on
#: every quota check; it expires with the grant, so the bonus stops on its end
#: date without any cleanup job.
_BONUS_KEY_PREFIX = "ai_quota:bonus:"


def bonus_messages_key(user_id: int | str) -> str:
    """Return the Redis key holding ``user_id``'s active bonus messages."""
    return f"{_BONUS_KEY_PREFIX}{user_id}"


class AIQuotaExceeded(AIUpstreamError):
    """The user's daily AI quota is spent; ``Retry-After`` = seconds to reset.

    This reuses the standard AI error machinery so the app switches on
    ``code == "ai_quota_exceeded"`` and shows the countdown from the header.
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            AIErrorCode.QUOTA_EXCEEDED,
            headers={"Retry-After": str(max(retry_after_seconds, 1))},
        )


def _seconds_until_utc_midnight(now: datetime) -> int:
    """Whole seconds until the next UTC midnight (``Retry-After`` value)."""
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC
    )
    return max(int((midnight - now).total_seconds()), 1)


def estimate_tokens(*, system_prompt: str, user_message: str, max_output_tokens: int) -> int:
    """Approximate the tokens (input + reserved output) a request will use.

    Input is estimated as ``chars / 4`` with a small per-section overhead;
    the reserved output is the provider's ``max_tokens`` cap, the same
    heuristic the client pacer uses to bound a request before it fires.
    """
    chars = len(system_prompt) + len(user_message)
    input_tokens = chars // 4 + 4
    return input_tokens + max(0, max_output_tokens)


class AIQuotaTracker:
    """Enforces per-user daily message/token budgets in a shared counter store."""

    def __init__(self, cache: RedisClient, settings: Settings) -> None:
        self.cache = cache
        self.settings = settings

    def _keys(self, user_id: int | str, day: str) -> tuple[str, str]:
        messages_key = f"ai_quota:messages:{user_id}:{day}"
        tokens_key = f"ai_quota:tokens:{user_id}:{day}"
        return messages_key, tokens_key

    async def _bonus_messages(self, user_id: int | str) -> int:
        """Return the user's active promo bonus messages (0 when none)."""
        value = coerce_int(await self.cache.get(bonus_messages_key(user_id))) or 0
        return max(value, 0)

    async def effective_limits(
        self, user_id: int | str, plan_tier: PlanTier | None = None
    ) -> PlanLimits:
        """Return the plan's limits plus any active promo bonus messages.

        A bonus only meaningfully raises a **finite** message ceiling, so it is
        ignored for unlimited plans (or when the daily message ceiling is
        itself unlimited).
        """
        limits = get_plan_limits(self.settings, plan_tier)
        if user_id is None or limits.unlimited or limits.daily_messages <= 0:
            return limits
        bonus = await self._bonus_messages(user_id)
        if bonus <= 0:
            return limits
        return replace(limits, daily_messages=limits.daily_messages + bonus)

    async def ensure_within_limits(
        self, user_id: int | str, plan_tier: PlanTier | None = None
    ) -> None:
        """Raise :class:`AIQuotaExceeded` when the user's budget is already spent.

        Peek-only: nothing is reserved here, so a provider that fails later
        does not burn today's allowance.  Returns silently when the budget
        is fine or the user is anonymous.
        """
        if user_id is None:
            return
        limits = await self.effective_limits(user_id, plan_tier)
        now = datetime.now(UTC)
        day = now.date().isoformat()
        messages_key, tokens_key = self._keys(user_id, day)

        used_messages = coerce_int(await self.cache.get(messages_key)) or 0
        used_tokens = coerce_int(await self.cache.get(tokens_key)) or 0

        if not limits.allows(messages=used_messages, tokens=used_tokens):
            seconds = _seconds_until_utc_midnight(now)
            logger.warning(
                "ai_quota_exceeded",
                extra={
                    "user_id": str(user_id),
                    "day": day,
                    "used_messages": used_messages,
                    "used_tokens": used_tokens,
                    "limit_messages": limits.daily_messages,
                    "limit_tokens": limits.daily_tokens,
                    "retry_after_seconds": seconds,
                },
            )
            raise AIQuotaExceeded(seconds)

    async def record_usage(
        self,
        user_id: int | str,
        *,
        tokens_used: int,
        plan_tier: PlanTier | None = None,
    ) -> None:
        """Charge one message and its token estimate against the user's day.

        The counters are created with a TTL aligned to the UTC-day boundary,
        so a user that returns tomorrow gets a fresh window automatically.
        Skips anonymous/system users.
        """
        if user_id is None:
            return
        limits = await self.effective_limits(user_id, plan_tier)
        now = datetime.now(UTC)
        day = now.date().isoformat()
        await self._record_plan_usage(plan_tier, day, tokens_used)
        if limits.unlimited:
            return
        messages_key, tokens_key = self._keys(user_id, day)
        reset_seconds = _seconds_until_utc_midnight(now)
        used_messages = await self._increment_with_ttl(messages_key, reset_seconds)
        used_tokens = await self._increment_with_ttl(tokens_key, reset_seconds, tokens_used)
        logger.info(
            "ai_quota_usage",
            extra={
                "user_id": str(user_id),
                "day": day,
                "used_messages": used_messages,
                "used_tokens": used_tokens,
                "limit_messages": limits.daily_messages,
                "limit_tokens": limits.daily_tokens,
            },
        )

    def _device_key(self, user_id: int | str, day: str) -> str:
        return f"ai_quota:device_actions:{user_id}:{day}"

    async def _record_plan_usage(self, plan_tier: PlanTier | None, day: str, tokens_used: int) -> None:
        """Bump the operator-facing per-plan aggregate for ``day``.

        These counters (``ai_metrics:usage:<plan>:<day>:messages|tokens``) feed
        the "Operações de IA" panel page.  They are written for every plan —
        including unlimited ones — and expire after ``AI_USAGE_RETENTION_DAYS``
        so the aggregate store stays bounded.
        """
        plan = (plan_tier or PlanTier.FREE).value
        retention = max(1, self.settings.ai_usage_retention_days) * 86400
        messages_key = f"ai_metrics:usage:{plan}:{day}:messages"
        tokens_key = f"ai_metrics:usage:{plan}:{day}:tokens"
        await self._increment_with_ttl(messages_key, retention)
        await self._increment_with_ttl(tokens_key, retention, max(tokens_used, 0))

    async def ensure_within_device_limits(
        self, user_id: int | str, plan_tier: PlanTier | None = None
    ) -> None:
        """Raise :class:`AIQuotaExceeded` when the daily device-command budget is spent.

        Device commands (turning lights on, running scenes, ...) are a separate
        budget from chat so an automation-heavy day does not eat the user's
        conversation quota.  A non-positive limit means unlimited.
        """
        if user_id is None:
            return
        limits = get_plan_limits(self.settings, plan_tier)
        if limits.daily_device_actions <= 0:
            return
        now = datetime.now(UTC)
        day = now.date().isoformat()
        key = self._device_key(user_id, day)
        used = coerce_int(await self.cache.get(key)) or 0
        if used >= limits.daily_device_actions:
            seconds = _seconds_until_utc_midnight(now)
            logger.warning(
                "ai_device_quota_exceeded",
                extra={
                    "user_id": str(user_id),
                    "day": day,
                    "used_device_actions": used,
                    "limit_device_actions": limits.daily_device_actions,
                    "retry_after_seconds": seconds,
                },
            )
            raise AIQuotaExceeded(seconds)

    async def record_device_action(
        self, user_id: int | str, plan_tier: PlanTier | None = None
    ) -> None:
        """Charge one device command against the user's day (no-op when unlimited)."""
        if user_id is None:
            return
        limits = get_plan_limits(self.settings, plan_tier)
        if limits.daily_device_actions <= 0:
            return
        now = datetime.now(UTC)
        day = now.date().isoformat()
        reset_seconds = _seconds_until_utc_midnight(now)
        await self._increment_with_ttl(self._device_key(user_id, day), reset_seconds)

    async def _increment_with_ttl(
        self, key: str, reset_seconds: int, amount: int = 1
    ) -> int:
        """Increment a day counter, giving it the reset TTL on first use.

        ``ttl`` is consulted after the increment: a freshly created key has
        no TTL (``-1``) and gets one; an existing key keeps its expiry so the
        window never shifts mid-day.
        """
        value = await self.cache.incrby(key, amount)
        ttl = await self.cache.ttl(key)
        if ttl < 0:
            await self.cache.expire(key, reset_seconds)
        return value
