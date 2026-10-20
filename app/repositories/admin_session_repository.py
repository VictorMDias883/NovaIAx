"""
Repository layer for :class:`app.models.admin_session.AdminSession`.

Only the hash of a session token is ever stored; lookups therefore go
through :meth:`AdminSessionRepository.get_by_token_hash`.  Mutating methods
``flush`` but do not commit — the service layer owns the transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_session import AdminSession


class AdminSessionRepository:
    """Data-access layer for admin console sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: int,
        token_hash: str,
        csrf_token: str,
        expires_at: datetime,
        last_seen_at: datetime,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AdminSession:
        """Insert a new session row and return it (not yet committed)."""
        record = AdminSession(
            user_id=user_id,
            token_hash=token_hash,
            csrf_token=csrf_token,
            expires_at=expires_at,
            last_seen_at=last_seen_at,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_by_token_hash(self, token_hash: str) -> AdminSession | None:
        """Return the session with the given token hash, if any."""
        result = await self.session.execute(
            select(AdminSession).where(AdminSession.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def touch(self, session_id: int, when: datetime) -> None:
        """Advance the session's idle clock."""
        await self.session.execute(
            update(AdminSession).where(AdminSession.id == session_id).values(last_seen_at=when)
        )
        await self.session.flush()

    async def revoke(self, session_id: int, when: datetime) -> None:
        """Mark a session revoked (logout or force-invalidation)."""
        await self.session.execute(
            update(AdminSession)
            .where(AdminSession.id == session_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=when)
        )
        await self.session.flush()

    async def revoke_all_for_user(self, user_id: int, when: datetime) -> int:
        """Revoke every live session of a user; returns the number revoked."""
        result = await self.session.execute(
            update(AdminSession)
            .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=when)
        )
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def purge_dead(self, before: datetime) -> int:
        """Delete sessions expired or revoked before ``before``."""
        result = await self.session.execute(
            delete(AdminSession).where(
                (AdminSession.expires_at < before)
                | (AdminSession.revoked_at.is_not(None) & (AdminSession.revoked_at < before))
            )
        )
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)
