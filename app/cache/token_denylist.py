"""
Redis-backed denylist for revoked JWT tokens.

When a user logs out, the ``jti`` (unique identifier) of their refresh
token is stored in Redis with a TTL matching the token's remaining
lifetime.  ``get_current_user`` and the refresh flow refuse any token
whose ``jti`` appears in the denylist, so a logged-out token can no
longer be used even if it has not yet expired.
"""

from app.cache.redis_client import RedisClient, RedisUnavailableError
from app.core.config import get_settings
from app.core.logging import get_logger
from app.exceptions.api_errors import APIError, APIErrorCode

logger = get_logger(__name__)


class TokenDenylist:
    """Track revoked token identifiers in Redis (with in-memory fallback).

    Each entry is stored under ``token_denylist:<jti>`` and automatically
    evicted by Redis once the token would have expired anyway, so entries
    never linger past their useful life.
    """

    KEY_PREFIX = "token_denylist:"

    def __init__(self, client: RedisClient | None = None) -> None:
        """Initialise the denylist with a Redis client.

        Args:
            client: Optional :class:`RedisClient` instance.  If omitted, a
                new one is created (honouring the configured Redis URL).
        """
        if client is None:
            settings = get_settings()
            # In production a revocation list that silently restarts empty
            # would re-validate logged-out tokens, so it fails closed there.
            client = RedisClient(settings, fail_closed=settings.is_production)
        self.client = client

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        """Mark a token as revoked for the given number of seconds.

        Args:
            jti: The token's unique identifier (from its payload).
            ttl_seconds: How long the revocation stays active, matching
                the remaining lifetime of the token.
        """
        try:
            await self.client.set(f"{self.KEY_PREFIX}{jti}", "revoked", ex=max(ttl_seconds, 1))
        except RedisUnavailableError as exc:
            raise _unavailable() from exc

    async def is_revoked(self, jti: str) -> bool:
        """Return ``True`` if the given ``jti`` is currently revoked.

        Args:
            jti: The token's unique identifier (from its payload).

        Returns:
            ``True`` if the identifier is present in the denylist.
        """
        try:
            return await self.client.get(f"{self.KEY_PREFIX}{jti}") is not None
        except RedisUnavailableError as exc:
            raise _unavailable() from exc


def _unavailable() -> APIError:
    """503 used when the revocation list cannot be consulted."""
    logger.error("token_denylist_unavailable")
    return APIError(
        503,
        "Serviço temporariamente indisponível. Tente novamente em instantes.",
        code=APIErrorCode.SERVICE_UNAVAILABLE,
    )


_denylist: TokenDenylist | None = None


def get_token_denylist() -> TokenDenylist:
    """Return the application-wide :class:`TokenDenylist` singleton.

    A single shared instance is used so that revocations made in one
    request (e.g. during logout) are visible to every other request
    (e.g. ``get_current_user``).  This matters both when Redis is
    available and for the in-memory fallback used in tests.
    """
    global _denylist
    if _denylist is None:
        _denylist = TokenDenylist()
    return _denylist
