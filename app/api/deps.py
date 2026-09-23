"""
FastAPI dependency-injection providers.

This module defines reusable dependencies that can be injected into
route handlers via FastAPI's ``Depends()`` mechanism.  Dependencies
are the recommended way to share logic (e.g. authentication, database
sessions, service instances) across multiple endpoints.

Key dependencies:
    - :func:`get_session` — provides a request-scoped :class:`AsyncSession`.
    - :func:`get_api_key_service` — provides an :class:`ApiKeyService`.
    - :func:`get_redis_client` — provides the shared :class:`RedisClient`
      instance (one per process, so cached state persists across requests).
    - :func:`get_current_user` — authenticates the request and returns
      the current user's identity (via JWT or API key).
    - :func:`require_admin` — requires the current user to be an admin.
"""

from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.cache.token_denylist import TokenDenylist
from app.cache.token_denylist import get_token_denylist as get_shared_token_denylist
from app.core.config import get_settings
from app.core.security import ApiKeyService, decode_token
from app.db.session import SessionLocal
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository

#: Process-wide Redis client shared by every dependency injection.
#: A single instance holds cross-request state (conversation history, AI
#: response cache, denylists).  Previously each request created a fresh
#: ``RedisClient``, so whenever real Redis was unreachable the in-memory
#: fallback (which is per-instance) lost the conversation between turns —
#: the assistants "forgot" the whole thread on the very next message.
_persistent_redis_client: RedisClient | None = None


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
    """Provide the process-wide :class:`RedisClient` instance.

    Created once and reused for the lifetime of the process.  Real Redis
    (or the in-memory fallback) therefore keeps state across requests —
    conversation history, the AI-response cache, denylists — instead of
    losing it whenever a fresh per-request client is constructed.  In
    tests, :meth:`RedisClient.reset_all_memory_stores` still isolates each
    test from the previous one.
    """
    global _persistent_redis_client
    if _persistent_redis_client is None:
        _persistent_redis_client = RedisClient()
    return _persistent_redis_client


async def get_token_denylist() -> TokenDenylist:
    """Dependency that provides the application-wide :class:`TokenDenylist`."""
    return get_shared_token_denylist()


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
    denylist: TokenDenylist = Depends(get_token_denylist),
) -> dict[str, object]:
    """Authenticate the current request and return the user identity.

    This dependency supports two authentication mechanisms:

    1. **Bearer JWT token** — The ``Authorization`` header must contain
       ``Bearer <jwt>``.  The token is decoded and verified.  Only
       tokens with ``type == "access"`` are accepted (refresh tokens
       are rejected).  Tokens whose ``jti`` has been revoked (e.g. via
       logout) are rejected with a 401.

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
        denylist: The :class:`TokenDenylist` used to reject revoked
            tokens (or ``None`` when called without DI).

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
    if not isinstance(denylist, TokenDenylist):
        denylist = get_shared_token_denylist()

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

        jti = payload.get("jti")
        if jti and await denylist.is_revoked(jti):
            raise HTTPException(status_code=401, detail="Token has been revoked")

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
        return {"id": "api-key", "full_name": "API Key", "email": "api-key", "role": UserRole.SERVICE.value}

    # --- No valid credentials provided ---
    raise HTTPException(status_code=401, detail="Authentication required")


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict[str, object]:
    """Require that the current user is an administrator."""
    if current_user.get("role") != UserRole.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return current_user


async def require_db_user(current_user: dict = Depends(get_current_user)) -> dict[str, object]:
    """Require an identity backed by a real database user.

    API-key identities (``role == SERVICE``) are transient and carry the
    sentinel id ``"api-key"`` instead of a numeric user ID.  Endpoints that
    load or create per-user data must therefore reject them with a 403
    instead of crashing with a ``ValueError`` when coercing ``"api-key"``
    into an ``int``.
    """
    if current_user.get("role") == UserRole.SERVICE.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API-key authentication cannot be used on this endpoint")
    return current_user


async def require_admin_db_role(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
) -> dict[str, object]:
    """Require an administrator, resolving the live role from the database.

    Unlike :func:`require_admin`, which trusts the role claim embedded in
    the JWT at login time, this dependency reloads the user so a recently
    demoted (or deleted) user loses ADMIN privileges immediately instead
    of keeping them for the remainder of the access token's lifetime.
    """
    if current_user.get("role") == UserRole.SERVICE.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    user = await _load_user(str(current_user["id"]), session)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    return {**current_user, "role": user.role.value}


def get_settings_dep() -> object:
    """Dependency that provides the :class:`Settings` singleton."""
    return get_settings()
