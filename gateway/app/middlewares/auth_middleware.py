from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from gateway.app.api.deps import get_current_user
from gateway.app.core.logging import get_logger

logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            path.startswith("/auth")
            or "/auth/" in path
            or path.startswith("/docs")
            or path.startswith("/openapi")
            or path == "/health"
            or "/api/v1/auth" in path
        ):
            return await call_next(request)

        try:
            await get_current_user(request=request)
        except HTTPException as exc:
            if exc.status_code in {401, 429}:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return JSONResponse(status_code=500, content={"detail": "Authentication failed"})
        except Exception as exc:
            logger.exception("Authentication middleware failed", extra={"path": request.url.path})
            return JSONResponse(status_code=500, content={"detail": "Authentication failed"})

        return await call_next(request)
