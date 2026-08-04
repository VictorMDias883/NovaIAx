"""
Rate-limiting middleware.

This middleware implements a simple **sliding-window** rate limiter.
For each client IP, it tracks the timestamps of recent requests within
a 60-second window and rejects requests that exceed the configured
limit.

Two rate limits are supported:
    - ``rate_limit_default`` (60 req/min) — applied to all endpoints.
    - ``rate_limit_ai`` (10 req/min) — applied to endpoints whose path
      contains ``/ai/`` (typically the proxied AI service, which is
      more expensive to serve).

The rate-limit state is stored in-memory (``defaultdict(list)``).  In a
multi-process or multi-worker deployment, this would need to be
backed by a shared store (e.g. Redis sorted sets, which the
:class:`RedisClient` already supports via ``zadd`` / ``zremrangebyscore``
/ ``zcard``).

When a rate limit is exceeded, the middleware returns a 429 response
with ``Retry-After`` and ``X-RateLimit-*`` headers, following common
API conventions.
"""

from collections import defaultdict
from time import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces per-client request-rate limits."""

    def __init__(self, app, *args, **kwargs):
        """Initialise the middleware.

        Args:
            app: The ASGI application (passed by Starlette).
            *args, **kwargs: Additional arguments forwarded to
                :class:`BaseHTTPMiddleware`.
        """
        super().__init__(app)
        self.settings = get_settings()
        # ``defaultdict(list)`` maps each client IP to a list of
        # request timestamps.  Using ``defaultdict`` avoids the need
        # to check for key existence before appending.
        self._requests = defaultdict(list)

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
        now = time()
        # Use the client's IP address as the rate-limit key.
        key = request.client.host if request.client else "unknown"

        # Select the appropriate limit: stricter for AI endpoints.
        limit = self.settings.rate_limit_ai if "/ai/" in request.url.path else self.settings.rate_limit_default

        # Prune timestamps that fall outside the 60-second sliding window.
        window = self._requests[key]
        window[:] = [ts for ts in window if now - ts < 60]

        # If the client has already made ``limit`` requests in the
        # current window, reject the request with a 429.
        if len(window) >= limit:
            logger.warning("Rate limit exceeded", extra={"ip": key, "path": request.url.path})
            response = JSONResponse(status_code=429, content={"detail": "Too Many Requests"})
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["Retry-After"] = "60"
            return response

        # Record this request's timestamp and forward it.
        window.append(now)
        response = await call_next(request)

        # Attach rate-limit metadata to the response so clients can
        # monitor their usage.
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(limit - len(window), 0))
        response.headers["Retry-After"] = "60"
        return response
