"""
Per-(email, IP) failed-login lockout backed by Redis (with in-memory fallback).

A succession of wrong passwords against a single address is the classic
credential-stuffing signal.  This limiter keeps a counter per **(email, IP)**
pair on ``login_failed:<email>:<ip>`` that only a *wrong* attempt increments;
the counter lives for ``login_failed_attempts_window_seconds`` from the first
failure and, once it reaches ``login_failed_attempts_threshold``, blocks
further logins for that pair with an HTTP 429 until the window rolls over.  A
successful login always clears the counter for the pair.

The counter is keyed on **email + IP**, deliberately *not* email alone: a
third party who knows a victim's address (a public fact) must not be able to
lock the real owner out by hammering the login endpoint from elsewhere.  The
attacker only exhausts the budget for their own (email, IP) pair, which stops
them without denying the victim service.  It complements (rather than
replaces) the per-IP bucket in the rate-limit middleware, which still guards
the endpoint's request throughput.
"""

from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings

#: Prefix for all per-(email, IP) failure counters (used by the
#: account-deletion cleanup, which wipes ``login_failed:*`` keys belonging to a
#: deleted user).
KEY_PREFIX = "login_failed:"


class LoginRateLimiter:
    """Count failed logins per (email, IP) pair and short-circuit repeat offenders."""

    def __init__(self, client: RedisClient | None = None, settings: Settings | None = None) -> None:
        """Initialise the limiter.

        Args:
            client: A :class:`RedisClient` instance.  If omitted, a new
                client (with its own in-memory fallback) is created.
            settings: The application settings.  If omitted, the global
                singleton is used.
        """
        self.client = client or RedisClient()
        self.settings = settings or get_settings()

    @staticmethod
    def _key(email: str, ip: str | None) -> str:
        """Return the counter key for an (email, IP) pair.

        ``ip`` may be ``None`` (e.g. a test or internal caller without a
        request context); it is normalised to the literal ``"unknown"`` so the
        pair is still scoped and never collapses back to a bare email key.
        """
        normalised_email = email.strip().lower()
        normalised_ip = (ip or "unknown").strip() or "unknown"
        return f"{KEY_PREFIX}{normalised_email}:{normalised_ip}"

    @property
    def _threshold(self) -> int:
        """The configured failure ceiling (0 disables the limiter)."""
        return self.settings.login_failed_attempts_threshold

    async def is_locked(self, email: str, ip: str | None = None) -> bool:
        """Return whether the (email, IP) pair has exhausted its failure budget."""
        if self._threshold <= 0:
            return False
        raw = await self.client.get(self._key(email, ip))
        if raw is None:
            return False
        try:
            failed = int(raw)
        except (TypeError, ValueError):
            return False
        return failed >= self._threshold

    async def on_failure(self, email: str, ip: str | None = None) -> None:
        """Register one failed attempt and start the window on the first."""
        key = self._key(email, ip)
        await self.client.incrby(key, 1)
        # ``incrby`` on a missing key creates it with no TTL; arm the window
        # exactly once so later failures do not keep sliding it forward.
        if await self.client.ttl(key) == -1:
            await self.client.expire(key, self.settings.login_failed_attempts_window_seconds)

    async def on_success(self, email: str, ip: str | None = None) -> None:
        """Clear the failure counter for the (email, IP) pair on a successful login."""
        await self.client.delete(self._key(email, ip))
