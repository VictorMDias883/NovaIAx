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

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.login_rate_limiter import LoginRateLimiter
from app.cache.redis_client import RedisClient
from app.cache.token_denylist import TokenDenylist, get_token_denylist
from app.commands.login_command import LoginCommand
from app.commands.register_user_command import RegisterUserCommand
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import (
    claim_int,
    create_access_token,
    create_refresh_token,
    decode_token,
    pwd_context,
)
from app.exceptions.api_errors import APIError, APIErrorCode
from app.models.consent import DEFAULT_CONSENT_TYPES
from app.models.user import UserRole
from app.repositories.user_repository import UserRepository
from app.services.privacy_service import ConsentService

logger = get_logger(__name__)


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Return whether ``exc`` was caused by a unique-constraint violation.

    Only a duplicate insert should be reported to the caller as a conflict;
    every other integrity problem (a missing foreign key, a NOT NULL
    violation, a bad type) is a genuine server-side fault and must keep
    surfacing as a 500 so it stays visible.

    The check inspects the driver-level message because the SQLAlchemy
    dialect-independent wrapper carries no structured violation code, and
    the wording is stable per backend: PostgreSQL reports "duplicate key
    value violates unique constraint", SQLite "UNIQUE constraint failed".
    """
    message = str(getattr(exc, "orig", exc)).lower()
    return "unique" in message or "duplicate key" in message


class AuthService:
    """Service for user registration and authentication.

    This class wraps the :class:`UserRepository` and adds business
    logic such as password hashing, duplicate-user checks, JWT token
    issuance, and refresh-token revocation on logout.
    """

    def __init__(
        self,
        session: AsyncSession,
        token_denylist: TokenDenylist | None = None,
        redis_client: RedisClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Initialise the service with a database session.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession` used
                for all database operations.
            token_denylist: Optional :class:`TokenDenylist` used to
                revoke refresh tokens on logout.  Defaults to a new
                instance backed by the configured Redis client.
            redis_client: Optional shared :class:`RedisClient` used by the
                failed-login limiter (routes pass the process-wide client;
                tests pass the patched in-memory one).
            settings: Optional :class:`Settings`; the global singleton is
                used when omitted.
        """
        self.session = session
        self.settings = settings or get_settings()
        self.token_denylist = token_denylist or get_token_denylist()
        self.login_rate_limiter = LoginRateLimiter(redis_client, self.settings)

    async def register(self, command: RegisterUserCommand) -> dict[str, Any]:
        """Register a new user.

        Rules:
            - If no admin exists and the environment permits bootstrap (see
              :attr:`Settings.first_registrant_becomes_admin`), the new user
              becomes ADMIN.
            - In production this bootstrap is always disabled, so the new
              user becomes USER regardless of how many users exist.
            - Otherwise the new user becomes USER.
            - The role is determined entirely by the backend; the client
              may not influence it.
            - The admin existence check is performed inside the same
              transaction as user creation.

        The duplicate-email check happens *after* the users table is locked.
        Checking first would leave a window in which two simultaneous
        registrations of the same address both see "no such user" and both
        attempt the insert, so one of them would fail on the unique
        constraint and surface as a 500 instead of a 409.  The
        :class:`IntegrityError` handler below is the second line of defence
        for backends that do not support ``LOCK TABLE`` (SQLite in tests,
        for instance).
        """
        repo = UserRepository(self.session)

        password_hash = pwd_context.hash(command.password)
        try:
            async with self.session.begin():
                await repo.lock_users_table()
                existing = await repo.get_by_email(command.email)
                if existing:
                    raise HTTPException(status_code=409, detail="Usuário já existe.")
                is_admin_present = await repo.exists_admin()
                # Only bootstrap the first account to ADMIN when the
                # environment allows it.  In production this is always
                # disabled, so the ADMIN role comes exclusively from the
                # bootstrap settings and a public deployment can never hand
                # the role to whoever happens to sign up first.
                if not is_admin_present and self.settings.first_registrant_becomes_admin:
                    role = UserRole.ADMIN
                else:
                    role = UserRole.USER
                user = await repo.create(
                    full_name=command.full_name,
                    email=command.email,
                    password_hash=password_hash,
                    role=role,
                    birth_date=command.birth_date,
                    commit=False,
                )
                # GDPR-style consent log: opening an account is the acceptance
                # of the terms-of-service and privacy policy, recorded inside
                # the same transaction so registration and consent can never
                # diverge.
                consent_service = ConsentService(self.session, self.settings)
                for consent_type in DEFAULT_CONSENT_TYPES:
                    await consent_service.record(
                        user.id,
                        consent_type,
                        commit=False,
                        accepted_at=datetime.now(UTC),
                    )
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            logger.info("Rejected a duplicate registration for %s (unique constraint)", command.email)
            raise HTTPException(status_code=409, detail="Usuário já existe.") from exc

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
            raise HTTPException(status_code=401, detail="Token de atualização inválido.") from exc

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        jti = payload.get("jti")
        if jti and await self.token_denylist.is_revoked(jti):
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        user_id = claim_int(payload, "sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        repo = UserRepository(self.session)
        user = await repo.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        if user.suspended_at is not None:
            # A suspended account cannot extend its session: reject the refresh
            # so the client is forced to re-authenticate (and fails there too).
            raise APIError(
                403,
                "Conta suspensa. Entre em contato com o suporte.",
                code=APIErrorCode.ACCOUNT_SUSPENDED,
            )

        issued_at = claim_int(payload, "iat")
        if issued_at is None:
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        valid_after = user.refresh_token_valid_after
        if valid_after.tzinfo is None:
            valid_after = valid_after.replace(tzinfo=UTC)
        # Full precision on purpose.  ``refresh_token_valid_after`` records
        # *when* the credentials changed, and a JWT ``iat`` is an integer by
        # specification, so the comparison is exact to the second: a token
        # minted at second N is rejected once the credentials changed a whole
        # second later, and accepted until it does.  The invalidation paths
        # (:meth:`app.services.user_service.UserService.change_password` et
        # al.) store a value *rounded up to the next whole second*, so even a
        # change made mid-second N turns off every token minted in second N
        # (``int()`` alone would truncate ``N.something`` back to ``N`` and
        # let the very token the change meant to kill survive its own second).
        # A brand-new account stores a second-truncated value (see
        # ``User.refresh_token_valid_after``) whose floor is its own second,
        # so this never rejects the first token a user ever receives.
        if issued_at < int(valid_after.timestamp()):
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        # Token rotation: revoke the presented refresh token before issuing
        # the new pair, so a leaked/replayed refresh token can only ever be
        # used once.
        exp = claim_int(payload, "exp")
        if jti and exp is not None:
            ttl = max(exp - int(datetime.now(tz=UTC).timestamp()), 1)
            await self.token_denylist.revoke(jti, ttl)

        return {
            "access_token": create_access_token(str(user.id), email=user.email, role=user.role.value),
            "refresh_token": create_refresh_token(str(user.id)),
        }

    async def logout(self, refresh_token: str, access_token: str | None = None) -> None:
        """Revoke a refresh token (and optionally an access token).

        The token's ``jti`` is stored in the denylist with a TTL that
        matches its remaining lifetime.  Once revoked, the token cannot
        be exchanged for a new pair via :meth:`refresh`.

        .. note::
            Access tokens are short-lived, so revoking them at logout is
            optional.  When supplied, the access ``jti`` is revoked too,
            meaning a subsequent call to ``GET /auth/me`` (or any
            protected route) with that token returns 401 immediately.

        Args:
            refresh_token: The refresh token to revoke.
            access_token: Optional access token to revoke as well.

        Raises:
            HTTPException(401): If the refresh token is invalid.
        """
        try:
            payload = decode_token(refresh_token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Token de atualização inválido.") from exc

        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        jti = payload.get("jti")
        exp = claim_int(payload, "exp")
        if not jti or exp is None:
            raise HTTPException(status_code=401, detail="Token de atualização inválido.")

        ttl = max(exp - int(datetime.now(tz=UTC).timestamp()), 1)
        await self.token_denylist.revoke(jti, ttl)

        if access_token:
            try:
                access_payload = decode_token(access_token)
            except Exception:
                access_payload = {}
            if access_payload.get("type") == "access":
                access_jti = access_payload.get("jti")
                access_exp = claim_int(access_payload, "exp")
                if access_jti and access_exp is not None:
                    access_ttl = max(access_exp - int(datetime.now(tz=UTC).timestamp()), 1)
                    await self.token_denylist.revoke(access_jti, access_ttl)
    async def login(self, command: LoginCommand, client_ip: str | None = None) -> dict[str, Any]:
        """Authenticate a user and issue tokens.

        Consecutive wrong passwords for the same address trip the failed-login
        limiter (:class:`app.cache.login_rate_limiter.LoginRateLimiter`): once
        ``login_failed_attempts_threshold`` failures accumulate inside the
        window, further attempts are refused with a 429 until the window rolls
        over.  A *successful* login always clears the counter, so a real owner
        is never locked out after a single typo.

        The counter is keyed on the **(email, client IP)** pair, so an attacker
        who only knows the victim's email cannot lock the account out from a
        different network: they exhaust their own pair's budget instead.

        The returned JWT includes the user's current role.  This role is
        a snapshot at login time and does not change for already-issued
        tokens.
        """
        if await self.login_rate_limiter.is_locked(command.email, client_ip):
            raise APIError(
                429,
                "Muitas tentativas de login. Tente novamente mais tarde.",
                code=APIErrorCode.RATE_LIMIT_EXCEEDED,
            )

        repo = UserRepository(self.session)
        user = await repo.get_by_email(command.email)
        if not user or not pwd_context.verify(command.password, user.password_hash):
            await self.login_rate_limiter.on_failure(command.email, client_ip)
            logger.info("Failed login attempt for %s", command.email)
            raise APIError(401, "Credenciais inválidas.", code=APIErrorCode.INVALID_CREDENTIALS)

        if user.suspended_at is not None:
            # Refuse to mint tokens for a suspended account, exactly like the
            # per-request guard in :func:`app.api.deps._load_user` refuses to
            # serve one.  Correct credentials are required first so the state
            # is only disclosed to the account owner.
            await self.login_rate_limiter.on_success(command.email, client_ip)
            raise APIError(
                403,
                "Conta suspensa. Entre em contato com o suporte.",
                code=APIErrorCode.ACCOUNT_SUSPENDED,
            )

        await self.login_rate_limiter.on_success(command.email, client_ip)
        # Record the login for the admin console's user detail page.  This is
        # metadata only and never feeds an authorization decision.
        user.last_login_at = datetime.now(UTC)
        await self.session.commit()
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
