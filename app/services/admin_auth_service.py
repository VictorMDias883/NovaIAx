"""
Authentication service for the operator console.

This is deliberately **separate** from :class:`app.services.auth_service.AuthService`:
the panel mints no JWTs, issues its own opaque cookie, and authenticates only
accounts whose *live* database role is ``ADMIN``.  It reuses the same password
hashing context and the same per-(email, IP) lockout as the mobile API, so a
succession of wrong panel passwords is throttled identically.

Session model:

* a random token is set as an httpOnly cookie; only its SHA-256 hash is stored;
* every session carries a CSRF token and both an idle clock
  (``last_seen_at``) and an absolute cap (``expires_at``);

* a session is refused the moment the account is deleted, demoted, or has its
  ``refresh_token_valid_after`` bumped (password/role change).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.login_rate_limiter import LoginRateLimiter
from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings
from app.models.user import User, UserRole
from app.repositories.admin_session_repository import AdminSessionRepository
from app.repositories.user_repository import UserRepository
from app.services.user_service import _password_matches
from app.utils.datetime_utils import ensure_aware
from app.utils.totp import verify as verify_totp

#: Generic failure message — identical for unknown email and wrong password so
#: the panel never reveals whether an account exists.
_INVALID_CREDENTIALS = "E-mail ou senha inválidos."
_INVALID_TOTP = "Código de verificação inválido."
_LOCKED = "Muitas tentativas. Tente novamente mais tarde."


def _hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest stored in place of the raw token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class AdminAuthService:
    """Credential verification and session lifecycle for the panel."""

    def __init__(
        self,
        session: AsyncSession,
        redis_client: RedisClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.redis_client = redis_client
        self.users = UserRepository(session)
        self.sessions = AdminSessionRepository(session)
        self._limiter: LoginRateLimiter | None = None

    @property
    def limiter(self) -> LoginRateLimiter:
        """Lazily build the shared lockout limiter."""
        if self._limiter is None:
            self._limiter = LoginRateLimiter(client=self.redis_client, settings=self.settings)
        return self._limiter

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def authenticate(
        self,
        *,
        email: str,
        password: str,
        totp_code: str | None,
        ip: str | None,
    ) -> dict[str, Any]:
        """Verify admin credentials, enforcing lockout and optional TOTP.

        Returns a small dict (``id``/``email``/``full_name``/``totp_enrolled``)
        on success.  Raises ``HTTPException`` on lockout (429) or bad
        credentials (401).  The caller then calls :meth:`start_session`.
        """
        email = email.strip()
        if await self.limiter.is_locked(email, ip):
            raise HTTPException(status_code=429, detail=_LOCKED)

        user = await self.users.get_by_email(email)
        valid = user is not None and user.role == UserRole.ADMIN and _password_matches(password, user.password_hash)
        if not valid:
            await self.limiter.on_failure(email, ip)
            raise HTTPException(status_code=401, detail=_INVALID_CREDENTIALS)

        assert user is not None  # narrowed by ``valid``
        if (
            self.settings.admin_require_totp
            and user.admin_totp_secret
            and not verify_totp(user.admin_totp_secret, totp_code or "")
        ):
            await self.limiter.on_failure(email, ip)
            raise HTTPException(status_code=401, detail=_INVALID_TOTP)

        await self.limiter.on_success(email, ip)
        return {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "totp_enrolled": bool(user.admin_totp_secret),
        }

    async def start_session(
        self,
        *,
        user_id: int,
        ip: str | None,
        user_agent: str | None,
    ) -> tuple[str, str]:
        """Create a session row and return ``(raw_token, csrf_token)``.

        The raw token is returned exactly once (to be set as the cookie); only
        its hash is persisted.
        """
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.settings.admin_session_absolute_hours)
        await self.sessions.create(
            user_id=user_id,
            token_hash=_hash_token(raw_token),
            csrf_token=csrf_token,
            expires_at=expires_at,
            last_seen_at=now,
            ip_address=ip,
            user_agent=(user_agent or "")[:300] or None,
        )
        await self.session.commit()
        return raw_token, csrf_token

    # ------------------------------------------------------------------
    # Session validation
    # ------------------------------------------------------------------
    async def resolve(self, raw_token: str | None, *, ip: str | None = None) -> dict[str, Any] | None:
        """Return the panel context for a valid cookie, or ``None``.

        Validates existence, revocation, absolute expiry and idle timeout,
        reloads the user and confirms the live ``ADMIN`` role and that the
        account has not invalidated this session by changing credentials.  On
        success the idle clock is advanced and the session committed.
        """
        if not raw_token:
            return None

        record = await self.sessions.get_by_token_hash(_hash_token(raw_token))
        now = datetime.now(UTC)
        if record is None:
            return None

        reason = self._rejection_reason(record, now)
        user = None if reason else await self.users.get_by_id(record.user_id)
        if reason is None and (user is None or user.role != UserRole.ADMIN):
            reason = "role"
        # Invalidate when the account's credentials/role changed after this
        # session was minted.  ``refresh_token_valid_after`` is deliberately
        # rounded up to the next whole second by ``UserService``; the 1-second
        # grace absorbs that rounding so a session created moments after the
        # change is not rejected (which would loop the admin at login).
        if (
            reason is None
            and user is not None
            and ensure_aware(user.refresh_token_valid_after)
            > ensure_aware(record.created_at) + timedelta(seconds=1)
        ):
            reason = "credentials"

        if reason is not None:
            await self.sessions.revoke(record.id, now)
            await self.session.commit()
            return None

        assert user is not None
        context = {
            "id": str(user.id),
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role.value,
            "session_id": record.id,
            "csrf_token": record.csrf_token,
            "totp_enrolled": bool(user.admin_totp_secret),
            "needs_totp_enrollment": self.settings.admin_require_totp and not user.admin_totp_secret,
        }
        await self.sessions.touch(record.id, now)
        await self.session.commit()
        return context

    def _rejection_reason(self, record: Any, now: datetime) -> str | None:
        """Classify why a session row is no longer usable (or ``None``)."""
        if record.revoked_at is not None:
            return "revoked"
        if ensure_aware(record.expires_at) <= now:
            return "expired"
        idle_deadline = ensure_aware(record.last_seen_at) + timedelta(minutes=self.settings.admin_session_idle_minutes)
        if idle_deadline <= now:
            return "idle"
        return None

    async def revoke(self, raw_token: str | None) -> None:
        """Revoke the session identified by ``raw_token`` (logout)."""
        if not raw_token:
            return
        record = await self.sessions.get_by_token_hash(_hash_token(raw_token))
        if record is not None and record.revoked_at is None:
            await self.sessions.revoke(record.id, datetime.now(UTC))
            await self.session.commit()

    # ------------------------------------------------------------------
    # TOTP enrolment
    # ------------------------------------------------------------------
    async def set_totp_secret(self, user_id: int, secret: str) -> None:
        """Persist an admin's TOTP secret (enrolment)."""
        user: User | None = await self.users.get_by_id(user_id, for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        user.admin_totp_secret = secret
        await self.session.commit()

    async def clear_totp_secret(self, user_id: int) -> None:
        """Remove an admin's TOTP secret (disable 2FA)."""
        user: User | None = await self.users.get_by_id(user_id, for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        user.admin_totp_secret = None
        await self.session.commit()


__all__ = ["AdminAuthService"]
