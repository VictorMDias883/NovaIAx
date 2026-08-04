"""
API v1 authentication endpoints (database-backed auth flow).

This router provides a full authentication flow backed by a real
database (SQLite in development).  Users are stored as records in the
``users`` table, and passwords are hashed with PBKDF2 before storage.

Endpoints:
    - POST /auth/register — Create a new user account, receive JWT tokens.
    - POST /auth/login    — Authenticate with email/password, receive JWT tokens.

Note: There is a parallel set of auth endpoints in
:mod:`app.api.v1.auth` that use a simpler in-memory auth flow.
Both routers share the ``/auth`` prefix, so their routes coexist.
"""

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.login_command import LoginCommand
from app.commands.register_user_command import RegisterUserCommand
from app.db.session import SessionLocal
from app.schemas.auth_schemas import AuthResponse, LoginRequest, RegisterUserRequest
from app.services.auth_service import AuthService

# Create a sub-router with the ``/auth`` prefix and ``auth`` tag.
router = APIRouter(prefix="/auth", tags=["auth"])


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    Uses an async context manager to ensure the session is properly
    closed after the request completes.  The session is yielded (not
    returned) so that FastAPI can manage its lifecycle.

    Yields:
        An open :class:`AsyncSession` instance.
    """
    async with SessionLocal() as session:
        yield session


@router.post("/register", response_model=AuthResponse)
async def register_user(payload: RegisterUserRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    """Register a new user account.

    The request body is validated by the :class:`RegisterUserRequest`
    Pydantic model (including password complexity rules).  The
    :class:`AuthService` then:
        1. Checks for duplicate emails (409 Conflict if found).
        2. Hashes the password.
        3. Creates the user record.
        4. Issues JWT access and refresh tokens.

    Args:
        payload: Validated request body with ``full_name``, ``email``,
            and ``password``.
        session: Database session (injected via :func:`get_session`).

    Returns:
        An :class:`AuthResponse` with the user data and JWT tokens.
    """
    # The ``async for ... break`` pattern extracts a single session
    # from the async generator.  This is equivalent to calling
    # ``SessionLocal()`` directly but reuses the same dependency.
    async for db_session in get_session():
        break
    service = AuthService(db_session)
    command = RegisterUserCommand(full_name=payload.full_name, email=str(payload.email), password=payload.password)
    result = await service.register(command)
    return AuthResponse(**result)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    """Authenticate a user and return JWT tokens.

    The :class:`AuthService` looks up the user by email, verifies the
    password, and issues JWT tokens on success.

    Args:
        payload: Validated request body with ``email`` and ``password``.
        session: Database session (injected via :func:`get_session`).

    Returns:
        An :class:`AuthResponse` with the user data and JWT tokens.

    Raises:
        HTTPException(401): If the email or password is invalid.
    """
    async for db_session in get_session():
        break
    service = AuthService(db_session)
    command = LoginCommand(email=str(payload.email), password=payload.password)
    result = await service.login(command)
    return AuthResponse(**result)
