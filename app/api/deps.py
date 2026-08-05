"""
FastAPI dependency-injection providers.

This module defines reusable dependencies that can be injected into
route handlers via FastAPI's ``Depends()`` mechanism.  Dependencies
are the recommended way to share logic (e.g. authentication, database
sessions, service instances) across multiple endpoints.

Key dependencies:
    - :func:`get_settings_dep` — provides the :class:`Settings` singleton.
    - :func:`get_auth_service` — provides an :class:`AuthService` instance.
    - :func:`get_redis_client` — provides a :class:`RedisClient` instance.
    - :func:`get_current_user` — authenticates the request and returns
      the current user's identity (via JWT or API key).
"""

from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.security import AuthService
from app.cache.redis_client import RedisClient


async def get_settings_dep() -> object:
    """Dependency that provides the :class:`Settings` singleton.

    Returns:
        The global :class:`Settings` instance.
    """
    return get_settings()


async def get_auth_service() -> AuthService:
    """Dependency that provides an :class:`AuthService` instance.

    A new instance is created for each request.  The service uses the
    in-memory user store (seeded with the default admin account) and
    a Redis client for API-key validation.
    """
    return AuthService()


async def get_redis_client() -> RedisClient:
    """Dependency that provides a :class:`RedisClient` instance.

    A new instance is created for each request.  The client lazily
    connects to Redis (or falls back to an in-memory store) on first
    use.
    """
    return RedisClient()


async def get_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    authorization: str | None = Header(default=None, alias="Authorization"),
    api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, object]:
    """Authenticate the current request and return the user identity.

    This dependency supports two authentication mechanisms:

    1. **Bearer JWT token** — The ``Authorization`` header must contain
       ``Bearer <jwt>``.  The token is decoded and verified.  Only
       tokens with ``type == "access"`` are accepted (refresh tokens
       are rejected).

    2. **API key** — The ``X-API-Key`` header must contain a valid API
       key.  The key is validated against the master key or a hash
       stored in Redis.

    If neither mechanism succeeds, a 401 Unauthorized is raised.

    Args:
        request: The incoming :class:`Request` (unused directly, but
            required so FastAPI can inject it).
        auth_service: The :class:`AuthService` used for token decoding
            and API-key validation.
        authorization: The ``Authorization`` header value (if present).
        api_key: The ``X-API-Key`` header value (if present).

    Returns:
        A dictionary with ``id``, ``username``, and ``roles`` keys
        representing the authenticated user.

    Raises:
        HTTPException(401): If authentication fails for any reason.
    """
    # Read headers directly from the request so that this function works
    # both when called as a FastAPI dependency (where Header params are
    # resolved by the DI container) and when called directly from the
    # AuthMiddleware (where Header params are NOT resolved).
    authorization = request.headers.get("Authorization")
    api_key = request.headers.get("x-api-key")

    # When called directly from the middleware (not through FastAPI's
    # dependency injection), ``auth_service`` will be the raw ``Depends``
    # sentinel object rather than an ``AuthService`` instance.  Detect
    # that situation and create a real instance on the fly.
    if not isinstance(auth_service, AuthService):
        auth_service = AuthService()

    # --- JWT Bearer token authentication ---
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]

        try:
            payload = auth_service.decode_token(token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
        # Only "access" tokens are accepted here; "refresh" tokens
        # must be used with the /auth/refresh endpoint instead.
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")

        return {"id": payload.get("sub"), "username": payload.get("sub"), "roles": []}

    # --- API key authentication ---
    if api_key and await auth_service.authenticate_api_key(api_key):
        return {"id": "api-key", "username": "api-key", "roles": ["service"]}

    # --- No valid credentials provided ---
    raise HTTPException(status_code=401, detail="Authentication required")
