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
    - ``/redoc`` (ReDoc UI) — it is rendered from the same public OpenAPI
      schema as ``/docs``, so gating it would only make the documentation
      harder to reach without protecting anything.
    - ``/health`` (liveness probe)
    - ``/admin`` and anything under it (the admin panel authenticates
      itself with its own httpOnly session cookie — see
      :mod:`app.api.admin`).  Every admin route enforces authorization
      internally, so the panel is not left open by this exemption.
    - ``/privacy``, ``/terms`` and ``/account-deletion`` (the public,
      server-rendered compliance pages and the account-deletion request
      form — see :mod:`app.api.public_pages`).  The form deliberately
      works without a logged-in account, because a user who wants to leave
      must be able to request it even if their credentials are lost.  Its
      own CSRF protection guards the write side.
    - ``/api/v1/billing/google/rtdn`` (the Google Play RTDN push endpoint).
      It performs its own authentication with the shared RTDN secret or a
      Google-signed OIDC token, so the JWT gate must not run first.

For all other paths, the middleware calls :func:`get_current_user`
to validate the request's credentials (JWT or API key).  If
authentication fails, a 401 (or 403 for a suspended account, or 429 for
rate-limit errors) is returned immediately without forwarding the request
to the route handler.

Note: Individual route handlers may also use the ``get_current_user``
dependency for finer-grained access control.  This middleware provides
a *first line of defence* — if a request passes the middleware, it is
guaranteed to be from an authenticated source.
"""

import re

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api.deps import get_current_user
from app.core.logging import get_logger
from app.core.security import ApiKeyService
from app.exceptions.api_errors import APIErrorCode, code_for_status

# Public path prefixes compiled as regex for efficient matching.  Prefix
# patterns are anchored with ``(?:/|$)`` so that, e.g., ``^/auth`` does not
# accidentally treat ``/auth-admin`` or ``/author`` as public.
_PUBLIC_PATH_PREFIXES = [
    re.compile(r"^/auth(?:/|$)"),
    re.compile(r"^/api/v1/auth(?:/|$)"),
    re.compile(r"^/docs(?:/|$)"),
    # The schema lives at ``/openapi.json``; Swagger and ReDoc fetch it from
    # the browser with a plain GET and no Authorization header, so it has to be
    # public or the API explorers render an empty page.  ``\.json`` is part of
    # the pattern because ``/openapi`` alone would never match it.
    re.compile(r"^/openapi(?:\.json)?(?:/|$)"),
    re.compile(r"^/redoc(?:/|$)"),
    re.compile(r"^/health$"),
    re.compile(r"^/admin(?:/|$)"),
    re.compile(r"^/privacy(?:/|$)"),
    re.compile(r"^/terms(?:/|$)"),
    re.compile(r"^/account-deletion(?:/|$)"),
    # Google Pub/Sub push notifications for Play billing.  This endpoint is
    # called by Google, not by the app, so it cannot present a JWT; it
    # authenticates the request itself with the shared RTDN secret or a
    # Google-signed OIDC token (see ``app.api.v1.subscription_router``).
    re.compile(r"^/api/v1/billing/google/rtdn$"),
    # Remote app configuration: the app fetches this before it has a token to
    # decide whether it may even talk to the backend (maintenance mode) and
    # which version/modules it should use.  It exposes no user data.
    re.compile(r"^/api/v1/app-config$"),
]


logger = get_logger(__name__)


def _is_public_path(path: str) -> bool:
    """Check if a path should skip authentication."""
    return any(pattern.match(path) for pattern in _PUBLIC_PATH_PREFIXES)


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
        if _is_public_path(path):
            return await call_next(request)

        # --- Protected paths: require authentication ---
        try:
            api_key_service = ApiKeyService()
            await get_current_user(request=request, api_key_service=api_key_service)
        except HTTPException as exc:
            if exc.status_code in {401, 403, 429}:
                code = getattr(exc, "code", None) or code_for_status(exc.status_code)
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": code})
            return JSONResponse(
                status_code=500,
                content={"detail": "Falha na autenticação.", "code": APIErrorCode.INTERNAL_ERROR.value},
            )
        except Exception:
            logger.exception("Authentication middleware failed", extra={"path": path})
            return JSONResponse(
                status_code=500,
                content={"detail": "Falha na autenticação.", "code": APIErrorCode.INTERNAL_ERROR.value},
            )

        # Authentication succeeded — forward the request.
        return await call_next(request)
