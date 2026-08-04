"""
Authentication middleware.

This middleware enforces authentication on all incoming requests
**except** for a set of public endpoints (auth, docs, health check).
It runs *before* the request reaches the route handler, providing a
centralised authentication gate.

Public paths (no authentication required):
    - ``/auth`` and anything under ``/auth/``
    - ``/api/v1/auth`` and anything under it
    - ``/docs`` and ``/openapi`` (Swagger UI and OpenAPI schema)
    - ``/health`` (liveness probe)

For all other paths, the middleware calls :func:`get_current_user`
to validate the request's credentials (JWT or API key).  If
authentication fails, a 401 (or 429 for rate-limit errors) is returned
immediately without forwarding the request to the route handler.

Note: Individual route handlers may also use the ``get_current_user``
dependency for finer-grained access control.  This middleware provides
a *first line of defence* — if a request passes the middleware, it is
guaranteed to be from an authenticated source.
"""

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api.deps import get_current_user
from app.core.logging import get_logger

logger = get_logger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces authentication on protected routes."""

    async def dispatch(self, request: Request, call_next):
        """Process each incoming request.

        Args:
            request: The incoming :class:`Request`.
            call_next: A callable that forwards the request to the
                next middleware or route handler.

        Returns:
            A :class:`Response` — either the result of ``call_next``
            (if the request is allowed) or an error response (if
            authentication fails).
        """
        path = request.url.path

        # --- Public paths: skip authentication ---
        # These endpoints are accessible without credentials.
        if (
            path.startswith("/auth")
            or "/auth/" in path
            or path.startswith("/docs")
            or path.startswith("/openapi")
            or path == "/health"
            or "/api/v1/auth" in path
        ):
            return await call_next(request)

        # --- Protected paths: require authentication ---
        try:
            # ``get_current_user`` is an async dependency that validates
            # the JWT or API key and returns the user identity.  If it
            # raises an ``HTTPException``, we catch it below.
            await get_current_user(request=request)
        except HTTPException as exc:
            # 401 (Unauthorized) and 429 (Too Many Requests) are expected
            # authentication/rate-limit failures — return them as-is.
            if exc.status_code in {401, 429}:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            # Any other HTTPException is unexpected — return a generic 500.
            return JSONResponse(status_code=500, content={"detail": "Authentication failed"})
        except Exception as exc:
            # Catch-all for unexpected errors (e.g. Redis connection
            # failures during API-key validation).  Log the full
            # traceback and return a 500.
            logger.exception("Authentication middleware failed", extra={"path": request.url.path})
            return JSONResponse(status_code=500, content={"detail": "Authentication failed"})

        # Authentication succeeded — forward the request.
        return await call_next(request)
