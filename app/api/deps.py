"""
FastAPI dependency-injection providers.

This module defines reusable dependencies that can be injected into
route handlers via FastAPI's ``Depends()`` mechanism.  Dependencies
are the recommended way to share logic (e.g. authentication, database
sessions, service instances) across multiple endpoints.

Key dependencies:
    - :func:`get_session` — provides a request-scoped :class:`AsyncSession`.
    - :func:`get_api_key_service` — provides an :class:`ApiKeyService`.
    - :func:`get_redis_client` — provides a :class:`RedisClient` instance.
    - :func:`get_current_user` — authenticates the request and returns
      the current user's identity (via JWT or API key).
    - :func:`require_admin` — requires the current user to be an admin.
"""

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.core.config import get_settings
from app.core.security import ApiKeyService, decode_token
from app.db.session import SessionLocal
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that yields a request-scoped database session.

    Uses an async context manager so the session is always closed after
    the request completes.
    """
    async with SessionLocal() as session:
        yield session


async def get_api_key_service() -> ApiKeyService:
    """Dependency that provides an :class:`ApiKeyService` instance.

    A new instance is created for each request.  The service lazily
    connects to Redis (or falls back to an in-memory store).
    """
    return ApiKeyService()


async def get_redis_client() -> RedisClient:
    """Dependency that provides a :class:`RedisClient` instance."""
    return RedisClient()


async def _load_user(user_id: str, session: AsyncSession | None) -> User:
    """Load a user by ID, opening a temporary session when needed.

    ``get_current_user`` is called both by FastAPI's DI container (which
    provides a real session) and directly by the auth middleware (which
    cannot resolve dependencies).  This helper normalises both paths.
    """
    if isinstance(session, AsyncSession):
        user = await UserRepository(session).get_by_id(int(user_id))
    else:
        async with SessionLocal() as temp_session:
            user = await UserRepository(temp_session).get_by_id(int(user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


async def get_current_user(
    request: Request,
    api_key_service: ApiKeyService = Depends(get_api_key_service),
    session: AsyncSession | None = Depends(get_session),
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
        request: The incoming :class:`Request`.
        api_key_service: The :class:`ApiKeyService` used for API-key
            validation.
        session: A request-scoped database session (or ``None`` when
            called outside FastAPI's DI container).

    Returns:
        A dictionary with ``id``, ``full_name``, ``email``, and ``role``
        keys representing the authenticated user.

    Raises:
        HTTPException(401): If authentication fails for any reason.
    """
    authorization = request.headers.get("Authorization")
    api_key = request.headers.get("x-api-key")

    # When called directly from the middleware (not through FastAPI's
    # dependency injection), ``api_key_service`` will be the raw
    # ``Depends`` sentinel object rather than an ``ApiKeyService``
    # instance.  Detect that situation and create a real instance.
    if not isinstance(api_key_service, ApiKeyService):
        api_key_service = ApiKeyService()

    # --- JWT Bearer token authentication ---
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]

        try:
            payload = decode_token(token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
        # Only "access" tokens are accepted here; "refresh" tokens
        # must be used with the /auth/refresh endpoint instead.
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")

        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        user = await _load_user(str(user_id), session)
        return {
            "id": str(user.id),
            "full_name": user.full_name,
            "email": user.email,
            "role": payload.get("role", user.role.value),
        }

    # --- API key authentication ---
    if api_key and await api_key_service.authenticate_api_key(api_key):
        return {"id": "api-key", "full_name": "API Key", "email": "api-key", "role": "SERVICE"}

    # --- No valid credentials provided ---
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict[str, object]:
    """Require that the current user is an administrator."""
    if current_user.get("role") != UserRole.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return current_user


def get_settings_dep() -> object:
    """Dependency that provides the :class:`Settings` singleton."""
    return get_settings()
