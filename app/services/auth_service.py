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

        Steps:
            1. Check if a user with the given email already exists.
               If so, raise a 409 Conflict.
            2. Hash the password with PBKDF2.
            3. Create the user record via the repository.
            4. Generate JWT access and refresh tokens.
            5. Return a dict containing the user data and tokens.

        Args:
            command: A :class:`RegisterUserCommand` with the user's
                full name, email, and plaintext password.

        Returns:
            A dictionary with ``user``, ``access_token``, and
            ``refresh_token`` keys.

        Raises:
            HTTPException(409): If the email is already registered.
        """
        repo = UserRepository(self.session)
        existing = await repo.get_by_email(command.email)
        if existing:
            raise HTTPException(status_code=409, detail="User already exists")

        password_hash = pwd_context.hash(command.password)
        user = await repo.create(full_name=command.full_name, email=command.email, password_hash=password_hash)
        return {
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "email": user.email,
            },
            "access_token": create_access_token(str(user.id)),
            "refresh_token": create_refresh_token(str(user.id)),
        }

    async def login(self, command: LoginCommand) -> dict[str, Any]:
        """Authenticate a user and issue tokens.

        Steps:
            1. Look up the user by email.
            2. Verify the password against the stored hash.
            3. If either check fails, raise a 401 Unauthorized.
            4. Generate JWT access and refresh tokens.
            5. Return a dict containing the user data and tokens.

        Args:
            command: A :class:`LoginCommand` with the user's email
                and plaintext password.

        Returns:
            A dictionary with ``user``, ``access_token``, and
            ``refresh_token`` keys.

        Raises:
            HTTPException(401): If the email or password is invalid.
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
            },
            "access_token": create_access_token(str(user.id)),
            "refresh_token": create_refresh_token(str(user.id)),
        }
