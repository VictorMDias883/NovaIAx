from collections import defaultdict
from time import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *args, **kwargs):
        super().__init__(app)
        self.settings = get_settings()
        self._requests = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        now = time()
        key = request.client.host if request.client else "unknown"
        limit = self.settings.rate_limit_ai if "/ai/" in request.url.path else self.settings.rate_limit_default
        window = self._requests[key]
        window[:] = [ts for ts in window if now - ts < 60]

        if len(window) >= limit:
            logger.warning("Rate limit exceeded", extra={"ip": key, "path": request.url.path})
            response = JSONResponse(status_code=429, content={"detail": "Too Many Requests"})
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["Retry-After"] = "60"
            return response

        window.append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(limit - len(window), 0))
        response.headers["Retry-After"] = "60"
        return response
