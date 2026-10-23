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

from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.cache.token_denylist import TokenDenylist
from app.cache.token_denylist import get_token_denylist as get_shared_token_denylist
from app.clients.ai_quota import AIQuotaTracker
from app.clients.ai_router import AIRouter, build_ai_router
from app.clients.ai_types import PlanTier
from app.clients.google_play_client import GooglePlayClient
from app.clients.plan_limits import get_plan_limits
from app.core.config import get_settings
from app.core.security import ApiKeyService, claim_int, coerce_int, decode_token
from app.db.session import SessionLocal
from app.exceptions.api_errors import APIError, APIErrorCode
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from app.services.app_config_service import suspended_detail
from app.services.subscription_service import Entitlement, SubscriptionService, enforce_feature

#: Process-wide Redis client shared by every dependency injection.
#: A single instance holds cross-request state (conversation history, AI
#: response cache, denylists).  Previously each request created a fresh
#: ``RedisClient``, so whenever real Redis was unreachable the in-memory
#: fallback (which is per-instance) lost the conversation between turns —
#: the assistants "forgot" the whole thread on the very next message.
_persistent_redis_client: RedisClient | None = None

#: Process-wide AI router shared by every request.
#: ``AIRouter`` owns the provider fleet and per-provider circuit breakers,
#: pacers and semaphores.  Those must be shared across requests to work at
#: all — a per-request router would reset breakers and pacing windows on
#: every call.  Tests replace this dependency with
#: ``app.dependency_overrides[get_ai_router]`` to inject fake providers.
_persistent_ai_router: AIRouter | None = None

#: Process-wide Google Play client.  It caches the service-account OAuth2
#: token and its HTTP connection pool, so sharing one instance across
#: requests avoids re-minting a token on every call.  Tests override this
#: dependency with an in-memory fake.
_persistent_google_play_client: GooglePlayClient | None = None

#: Key under which the authenticated identity is memoised in the request
#: state.  Starlette keeps ``request.state`` in ``scope["state"]``, which is
#: shared by every ``Request`` object built for the same ASGI scope — so the
#: object the middleware authenticates is the same one FastAPI hands to the
#: dependency.
_IDENTITY_STATE_KEY = "auth_identity"


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


async def get_ai_router(redis_client: RedisClient = Depends(get_redis_client)) -> AIRouter:
    """Provide the process-wide :class:`AIRouter` instance.

    Built once from settings and reused for the lifetime of the process so
    the per-provider circuit breakers and pacing windows span requests.
    Routes must never construct an :class:`app.clients.ai_client.AIClient`
    directly; they receive the router here instead.  Tests override this
    dependency to inject a fake router / fake providers.
    """
    global _persistent_ai_router
    if _persistent_ai_router is None:
        _persistent_ai_router = build_ai_router(get_settings(), redis_client)
    return _persistent_ai_router


async def get_token_denylist() -> TokenDenylist:
    """Dependency that provides the application-wide :class:`TokenDenylist`."""
    return get_shared_token_denylist()


async def _load_user(user_id: object, session: AsyncSession | None) -> User:
    """Load a user by ID, opening a temporary session when needed.

    ``get_current_user`` is called both by FastAPI's DI container (which
    provides a real session) and directly by the auth middleware (which
    cannot resolve dependencies).  This helper normalises both paths.

    Args:
        user_id: The ``sub`` claim, as a string or an int.  A value that is
            not a whole number (for example the API-key sentinel, or a token
            whose claim was tampered with) yields a 401 instead of an
            unhandled ``ValueError``.
        session: A real session, or ``None`` to open a temporary one.
    """
    user_id_int = coerce_int(user_id)
    if user_id_int is None:
        raise HTTPException(status_code=401, detail="Token inválido.")

    if isinstance(session, AsyncSession):
        user = await UserRepository(session).get_by_id(user_id_int)
    else:
        async with SessionLocal() as temp_session:
            user = await UserRepository(temp_session).get_by_id(user_id_int)
    if user is None:
        raise HTTPException(status_code=401, detail="Token inválido.")
    if user.suspended_at is not None:
        # A suspended account is rejected on every request, centrally, so no
        # route can accidentally serve it.  Access tokens already issued stop
        # working the moment the operator suspends the account.
        raise await _suspended_error(session)
    return user


async def _suspended_error(session: AsyncSession | None) -> APIError:
    """Build the ``account_suspended`` 403 with the configured support contact.

    The detail is rendered by
    :func:`~app.services.app_config_service.suspended_detail`.  The middleware
    path (``session is None``) opens its own session, because the config must
    be read before any temporary session is closed.
    """
    if isinstance(session, AsyncSession):
        detail = await suspended_detail(session)
    else:
        async with SessionLocal() as temp_session:
            detail = await suspended_detail(temp_session)
    return APIError(403, detail, code=APIErrorCode.ACCOUNT_SUSPENDED)


async def _release_read_transaction(session: AsyncSession | None) -> None:
    """Close the implicit transaction that a read has opened on [session].

    SQLAlchemy's "begin once" behaviour means any ``execute()`` autobegins a
    transaction.  The authentication dependencies below therefore leave the
    request-scoped session already in a transaction, and a service that
    guards its writes with ``async with session.begin():`` (as
    :class:`~app.services.user_service.UserService` does) would otherwise
    fail with *"A transaction is already begun on this Session"*.

    Rolling back here hands the session back clean, so the service can own
    its own write transaction — the same fix
    :func:`app.api.admin.require_admin_panel` already applies for the web
    panel's cookie-based auth.

    .. important::
       Callers must copy everything they need out of the ORM instance
       *before* calling this: the rollback expires that instance, and any
       later attribute access would trigger a lazy refresh that raises
       ``MissingGreenlet``.

    No-op when no real session is in play.
    """
    if isinstance(session, AsyncSession) and session.in_transaction():
        await session.rollback()


def _cached_identity(request: Request) -> dict[str, object] | None:
    """Return the identity already resolved for this request, if any.

    Repeated dependency paths reuse the identity resolved earlier in this
    request, alongside FastAPI's per-request dependency cache.
    """
    cached = request.scope.get("state", {}).get(_IDENTITY_STATE_KEY)
    return cached if isinstance(cached, dict) else None


def _remember_identity(request: Request, identity: dict[str, object]) -> dict[str, object]:
    """Memoise ``identity`` on the request state and return it."""
    request.scope.setdefault("state", {})[_IDENTITY_STATE_KEY] = identity
    return identity


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

    Repeated dependency paths reuse the identity already resolved for this
    request instead of repeating token verification and user lookup.

    Args:
        request: The incoming :class:`Request`.
        api_key_service: The :class:`ApiKeyService` used for API-key
            validation.
        session: A request-scoped database session.
        denylist: The :class:`TokenDenylist` used to reject revoked
            tokens.

    Returns:
        A dictionary with ``id``, ``full_name``, ``email``, and ``role``
        keys representing the authenticated user.

    Raises:
        HTTPException(401): If authentication fails for any reason.
    """
    already_authenticated = _cached_identity(request)
    if already_authenticated is not None:
        return already_authenticated

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
            raise HTTPException(status_code=401, detail="Token inválido.") from exc
        # Only "access" tokens are accepted here; "refresh" tokens
        # must be used with the /auth/refresh endpoint instead.
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Tipo de token inválido.")

        jti = payload.get("jti")
        if jti and await denylist.is_revoked(jti):
            raise HTTPException(status_code=401, detail="Token revogado.")

        # Reject a token whose ``sub`` is missing or non-numeric here rather
        # than letting ``int()`` raise deep inside the user lookup.
        user_id = claim_int(payload, "sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token inválido.")

        user = await _load_user(user_id, session)
        # Copy the identity out of the ORM instance before releasing the read
        # transaction (see :func:`_release_read_transaction`).
        identity = {
            "id": str(user.id),
            "full_name": user.full_name,
            "email": user.email,
            "role": payload.get("role", user.role.value),
        }
        await _release_read_transaction(session)
        return _remember_identity(request, identity)

    # --- API key authentication ---
    if api_key and await api_key_service.authenticate_api_key(api_key):
        return _remember_identity(
            request,
            {"id": "api-key", "full_name": "API Key", "email": "api-key", "role": UserRole.SERVICE.value},
        )

    # --- No valid credentials provided ---
    raise HTTPException(status_code=401, detail="Autenticação necessária.")


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict[str, object]:
    """Require that the current user is an administrator."""
    if current_user.get("role") != UserRole.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Não autorizado.")
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A autenticação por API key não pode ser usada neste endpoint.")
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
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Não autorizado.")
    user = await _load_user(current_user["id"], session)
    # Read the live role out of the ORM instance before releasing the read
    # transaction (see :func:`_release_read_transaction`), otherwise this
    # comparison would trigger a lazy refresh and the service layer's own
    # ``session.begin()`` would collide with the auth read.
    role = user.role
    await _release_read_transaction(session)
    if role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Não autorizado.")
    return {**current_user, "role": role.value}


def get_settings_dep() -> object:
    """Dependency that provides the :class:`Settings` singleton."""
    return get_settings()


async def get_google_play_client() -> GooglePlayClient:
    """Provide the process-wide :class:`GooglePlayClient`.

    Built once and reused so the OAuth2 access token and connection pool are
    shared.  Tests override this dependency with a fake gateway to avoid any
    network access.
    """
    global _persistent_google_play_client
    if _persistent_google_play_client is None:
        _persistent_google_play_client = GooglePlayClient(get_settings())
    return _persistent_google_play_client


async def get_ai_quota_tracker(
    redis_client: RedisClient = Depends(get_redis_client),
) -> AIQuotaTracker:
    """Provide an :class:`AIQuotaTracker` bound to the shared Redis client."""
    return AIQuotaTracker(redis_client, get_settings())


def _free_entitlement() -> Entitlement:
    """The implicit entitlement for identities with no database account."""
    settings = get_settings()
    limits = get_plan_limits(settings, PlanTier.FREE)
    return Entitlement(
        plan=PlanTier.FREE,
        limits=limits,
        status=None,
        is_trial=False,
        active=True,
        subscription_inactive=False,
        has_subscription=False,
        product_id=None,
        expires_at=None,
        auto_renewing=False,
    )


async def get_entitlement(
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Entitlement:
    """Resolve the caller's entitlement **without** enforcing a feature gate.

    AI routes that any plan may use (objective creation, the objective
    assistant) still have to know the caller's tier so the daily quota is
    charged against the right ceiling instead of always the free one.  Unlike
    :func:`require_plan`, this never raises a paywall.
    """
    if current_user.get("role") == UserRole.SERVICE.value:
        return _free_entitlement()
    entitlement = await SubscriptionService(session).resolve_entitlement(int(str(current_user["id"])))
    await _release_read_transaction(session)
    return entitlement


def require_plan(feature: str) -> Callable[..., Coroutine[Any, Any, Entitlement]]:
    """Build a dependency that requires ``feature`` on the caller's plan.

    Returns the resolved :class:`Entitlement` so a route can read the plan and
    its limits (for example to thread the tier into the AI quota).  API-key
    (``SERVICE``) identities have no per-user plan and are granted the free
    baseline without a database lookup, so machine-to-machine call really
    still works for free features.

    Raises :class:`~app.exceptions.plan_errors.PlanRequiredError` (402 when an
    upgrade would unlock the feature, 403 when a subscription needs renewing).
    """

    async def _dependency(
        current_user: dict = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> Entitlement:
        if current_user.get("role") == UserRole.SERVICE.value:
            entitlement = _free_entitlement()
        else:
            entitlement = await SubscriptionService(session).resolve_entitlement(
                int(str(current_user["id"]))
            )
            # Hand the session back clean so the route's service can own its
            # own write transaction (see :func:`_release_read_transaction`).
            await _release_read_transaction(session)
        enforce_feature(entitlement, feature)
        return entitlement

    return _dependency
