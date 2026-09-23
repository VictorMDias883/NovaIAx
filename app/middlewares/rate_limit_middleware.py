"""
Rate-limiting middleware.

This middleware implements a simple **sliding-window** rate limiter
backed by Redis sorted sets.  For each client IP, it tracks request
timestamps in a Redis sorted set, allowing the limiter to work
correctly across multiple workers/processes.

Two rate limits are supported:
    - ``rate_limit_default`` (60 req/min) — applied to all endpoints.
    - ``rate_limit_ai`` (10 req/min) — applied to AI endpoints (paths
      containing ``/ai/`` or ``/agents/general``), which are more
      expensive to serve.

The sliding window is 60 seconds.  Timestamps outside this window
are removed using ``zremrangebyscore`` before checking the count.
"""

from time import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.cache.redis_client import RedisClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.network import get_client_ip

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces per-client request-rate limits using Redis."""

    def __init__(self, app, *args, **kwargs):
        """Initialise the middleware.

        Args:
            app: The ASGI application (passed by Starlette).
            *args, **kwargs: Additional arguments forwarded to
                :class:`BaseHTTPMiddleware`.
        """
        super().__init__(app)
        self.settings = get_settings()
        self.redis_client = RedisClient(self.settings)

    async def dispatch(self, request: Request, call_next):
        """Check the rate limit and either forward or reject the request.

        Args:
            request: The incoming :class:`Request`.
            call_next: A callable that forwards the request to the
                next middleware or route handler.

        Returns:
            A :class:`Response` — either the downstream response (with
            rate-limit headers) or a 429 error response.
        """
        # Exempt health-check probes from rate limiting entirely so a burst
        # of orchestrator/probe requests can never be rejected with a 429.
        if request.url.path == "/health":
            return await call_next(request)

        now = time()
        # Use the real client IP as the rate-limit key.  ``get_client_ip``
        # reads ``X-Forwarded-For`` (only when a trusted reverse proxy is
        # configured) so that all requests behind the Render edge proxy are
        # bucketed per user instead of collapsing into one shared global
        # limit.
        client_ip = get_client_ip(request, self.settings)
        key = f"rate_limit:{client_ip}"

        # Select the appropriate limit: stricter for AI endpoints.
        path = request.url.path
        limit = (
            self.settings.rate_limit_ai
            if "/ai/" in path or "/agents/general" in path or "/objectives/assistant" in path
            else self.settings.rate_limit_default
        )

        # Prune timestamps that fall outside the 60-second sliding window
        # and count remaining members in the window. If Redis fails for any
        # reason (unreachable, event-loop mismatch in tests, etc.), fall
        # back to a conservative behavior that does not block the request.
        try:
            await self.redis_client.zremrangebyscore(key, 0, now - 60)
            window_size = await self.redis_client.zcard(key)
        except Exception:
            # Log the failure and continue with window_size=0 so the
            # request is not rejected due to infrastructure issues.
            logger.exception("Redis error in rate limiter — falling back to in-memory behavior")
            window_size = 0

        # If the client has already made ``limit`` requests in the
        # current window, reject the request with a 429.
        if window_size >= limit:
            logger.warning("Rate limit exceeded", extra={"ip": key, "path": request.url.path})
            response = JSONResponse(status_code=429, content={"detail": "Too Many Requests"})
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["Retry-After"] = "60"
            return response

        # Record this request's timestamp in the sorted set (score = timestamp).
        try:
            await self.redis_client.zadd(key, {str(now): now})
        except Exception:
            logger.exception("Failed to record rate-limit event in Redis — continuing without persistence")

        response = await call_next(request)

        # Attach rate-limit metadata to the response so clients can
        # monitor their usage.
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(limit - window_size - 1, 0))
        response.headers["Retry-After"] = "60"
        return response
