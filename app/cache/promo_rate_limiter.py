"""
Fixed-window rate limiter for promo-code redemption.

Redeeming is cheap but abusable: without a limit a user could spam guesses at
codes (brute-force) or repeatedly trigger grants.  The limiter keeps two
independent counters per window:

* ``promo_redeem:user:<user_id>`` — stops one account burning its own budget;
* ``promo_redeem:ip:<ip>`` — stops one source hammering the endpoint.

Both must stay under their configured ceilings.  The window is armed on the
first hit and never slid forward, so it behaves as a fixed window rather than a
rolling one.
"""

from fastapi import HTTPException

from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings


class PromoRedeemRateLimiter:
    """Count redemption attempts per user and per IP inside one window."""

    def __init__(self, client: RedisClient | None = None, settings: Settings | None = None) -> None:
        self.client = client or RedisClient()
        self.settings = settings or get_settings()

    async def _hit(self, key: str, limit: int, window: int) -> bool:
        """Increment ``key`` and return True when its ceiling is exceeded."""
        if limit <= 0:
            return False
        current = await self.client.incrby(key, 1)
        if await self.client.ttl(key) == -1:
            await self.client.expire(key, window)
        return current > limit

    async def check(self, user_id: int, ip: str | None) -> None:
        """Raise ``429`` when either the user or the IP budget is spent."""
        window = self.settings.promo_redeem_window_seconds
        blocked = await self._hit(
            f"promo_redeem:user:{user_id}", self.settings.promo_redeem_user_limit, window
        )
        normalised_ip = (ip or "unknown").strip() or "unknown"
        blocked = (
            await self._hit(
                f"promo_redeem:ip:{normalised_ip}", self.settings.promo_redeem_ip_limit, window
            )
            or blocked
        )
        if blocked:
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas de resgate. Aguarde alguns instantes.",
            )


__all__ = ["PromoRedeemRateLimiter"]
