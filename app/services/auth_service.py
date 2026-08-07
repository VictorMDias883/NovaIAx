"""
Service layer for authentication (database-backed).

This :class:`AuthService` is the **database-backed** counterpart to the
in-memory :class:`AuthService` in :mod:`app.core.security`.  It is used
by the v2 auth endpoints in :mod:`app.api.v1.auth_router` and performs
real user registration and login against the SQLite database.

The service layer sits between the API (router) layer and the
repository layer:
    Router → Command → Service → Repository → Database

Responsibilities:
    - Validate that a user does not already exist before registration.
    - Hash passwords with PBKDF2 before storing them.
    - Verify passwords during login.
    - Issue JWT access and refresh tokens on successful authentication.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.login_command import LoginCommand
from app.commands.register_user_command import RegisterUserCommand
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.models.user import UserRole
from app.repositories.user_repository import UserRepository

# Password hashing context — same configuration as in
# :mod:`app.core.security`.  PBKDF2 with SHA-256 is used because it
# is built into Python's standard library (no external C dependencies).
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


class AuthService:
    """Service for user registration and authentication.

    This class wraps the :class:`UserRepository` and adds business
    logic such as password hashing, duplicate-user checks, and JWT
    token issuance.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialise the service with a database session.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession` used
                for all database operations.
        """
        self.session = session

    async def register(self, command: RegisterUserCommand) -> dict[str, Any]:
        """Register a new user.

        Rules:
            - If no admin exists at registration time, the new user
              becomes ADMIN.
            - Otherwise the new user becomes USER.
            - The role is determined entirely by the backend; the client
              may not influence it.
            - The admin existence check is performed inside the same
              transaction as user creation.
        """
        repo = UserRepository(self.session)
        

        password_hash = pwd_context.hash(command.password)
        async with self.session.begin():
            existing = await repo.get_by_email(command.email)
            if existing:
                raise HTTPException(status_code=409, detail="User already exists")
            await repo.lock_users_table()
            is_admin_present = await repo.exists_admin()
            role = UserRole.USER if is_admin_present else UserRole.ADMIN
            user = await repo.create(
                full_name=command.full_name,
                email=command.email,
                password_hash=password_hash,
                role=role,
                commit=False,
            )

        return {
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "email": user.email,
                "role": user.role.value,
            },
            "access_token": create_access_token(str(user.id), email=user.email, role=user.role.value),
            "refresh_token": create_refresh_token(str(user.id)),
        }
    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        """Refresh access and refresh tokens using a valid refresh token."""
        try:
            payload = decode_token(refresh_token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid refresh token") from exc

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        repo = UserRepository(self.session)
        user = await repo.get_by_id(int(user_id))
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        issued_at = payload.get("iat")
        if issued_at is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        if datetime.fromtimestamp(int(issued_at), tz=timezone.utc) < user.refresh_token_valid_after:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        return {
            "access_token": create_access_token(str(user.id), email=user.email, role=user.role.value),
            "refresh_token": create_refresh_token(str(user.id)),
        }
    async def login(self, command: LoginCommand) -> dict[str, Any]:
        """Authenticate a user and issue tokens.

        The returned JWT includes the user's current role.  This role is
        a snapshot at login time and does not change for already-issued
        tokens.
        """
        repo = UserRepository(self.session)
        user = await repo.get_by_email(command.email)
        if not user or not pwd_context.verify(command.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        return {
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "email": user.email,
                "role": user.role.value,
            },
            "access_token": create_access_token(str(user.id), email=user.email, role=user.role.value),
            "refresh_token": create_refresh_token(str(user.id)),
        }
