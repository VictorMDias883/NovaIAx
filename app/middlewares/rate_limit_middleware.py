"""
Rate-limiting middleware.

This middleware implements a simple **sliding-window** rate limiter
backed by Redis sorted sets.  For each client IP, it tracks request
timestamps in a Redis sorted set, allowing the limiter to work
correctly across multiple workers/processes.

Two rate limits are supported:
    - ``rate_limit_default`` (60 req/min) — applied to all endpoints.
    - ``rate_limit_ai`` (10 req/min) — applied to endpoints that trigger an
      AI provider call (``/ai/``, ``/agents/general``, ``/agents/device``,
      ``/agents/chat``, ``/objectives/assistant``, ``/objectives/register`` and
      ``/objectives/{id}/roadmap/renew``), which are expensive to serve and
      count **against the provider's own** requests/tokens-per-minute quota
      (not just ours).

AI endpoints are bucketed **per authenticated user id** (``rate_limit_ai_user``,
8 req/min) whenever the request carries a valid access token, and per client IP
(``rate_limit_ai``) otherwise.  Keying the expensive bucket on the user means a
single account cannot multiply its share of the provider quota by rotating
client IPs, and users sharing one NAT/office address each get their own budget
instead of competing for a single IP bucket.  Ordinary (non-AI) traffic stays
purely per-IP.

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
from app.core.security import claim_int, decode_token
from app.exceptions.api_errors import APIErrorCode

logger = get_logger(__name__)

#: Path fragments that identify endpoints which make an AI provider call
#: (subject to the shared strict rate-limit bucket, ``rate_limit_ai``).
_AI_ENDPOINT_PATH_FRAGMENTS = (
    "/ai/",
    "/agents/general",
    "/agents/device",
    "/agents/chat",
    "/objectives/assistant",
    "/objectives/register",
    "/roadmap/renew",
    "/me/weekly-plan",
)


def is_ai_rate_limited(path: str) -> bool:
    """Return ``True`` when ``path`` belongs to the strict AI rate-limit bucket.

    Every matched endpoint triggers at least one call to the AI provider, so
    they share the tighter bucket — this keeps the aggregate volume under the
    provider's own quota (per-API-key RPM/TPM), which the per-IP limiter alone
    cannot guarantee.
    """
    return any(fragment in path for fragment in _AI_ENDPOINT_PATH_FRAGMENTS)


def extract_user_id(request: Request) -> int | None:
    """Return the authenticated user id carried by the request, if any.

    The rate-limit middleware runs *before* authentication, so it cannot read
    ``request.state``.  It therefore inspects the ``Authorization: Bearer``
    header itself and decodes the JWT to obtain the ``sub`` claim.  Only a
    well-formed ``access`` token with a numeric ``sub`` yields an id; anything
    else (missing header, refresh token, revoked/expired/invalid signature)
    falls back to ``None`` so the caller buckets by IP instead. Authentication
    itself is enforced later by the protected route's FastAPI dependency.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("type") != "access":
        return None
    return claim_int(payload, "sub")


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

        # Select the appropriate bucket: stricter for AI endpoints, and a
        # SEPARATE counter.  Both buckets used one sorted set keyed only by IP,
        # so ordinary traffic filled it and then the stricter AI limit was
        # applied to that same total — a user who made ten normal requests in a
        # minute was refused the assistant, and vice versa an AI call counted
        # against the general allowance.  Each bucket now has its own window.
        path = request.url.path
        is_ai = is_ai_rate_limited(path)
        if is_ai:
            # AI traffic is bucketed per authenticated user so one account
            # cannot amplify its provider-quota share by rotating IPs; only
            # unauthenticated AI requests (no/invalid access token) fall back
            # to the per-IP bucket.
            user_id = extract_user_id(request)
            if user_id is not None:
                limit = self.settings.rate_limit_ai_user
                key = f"rate_limit:ai:user:{user_id}"
            else:
                limit = self.settings.rate_limit_ai
                key = f"rate_limit:ai:{client_ip}"
        else:
            limit = self.settings.rate_limit_default
            key = f"rate_limit:general:{client_ip}"

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
            response = JSONResponse(
                status_code=429,
                content={
                    "detail": "Muitas requisições. Tente novamente em instantes.",
                    "code": APIErrorCode.RATE_LIMIT_EXCEEDED.value,
                },
            )
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
