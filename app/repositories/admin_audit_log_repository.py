"""
Repository layer for :class:`app.models.admin_audit_log.AdminAuditLog`.

Reads support optional filters (admin, action, entity, date range) and
pagination; writes are append-only — the audit trail is never updated or
deleted by application code.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_audit_log import AdminAuditLog


class AdminAuditLogRepository:
    """Data-access layer for the admin audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        admin_id: int | None,
        admin_email: str | None,
        action: str,
        entity: str | None,
        entity_id: str | None,
        before: dict | None,
        after: dict | None,
        ip_address: str | None,
    ) -> AdminAuditLog:
        """Insert one audit row and return it (not yet committed)."""
        record = AdminAuditLog(
            admin_id=admin_id,
            admin_email=admin_email,
            action=action,
            entity=entity,
            entity_id=entity_id,
            before=before,
            after=after,
            ip_address=ip_address,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_entries(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        admin_id: int | None = None,
        action: str | None = None,
        entity: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[AdminAuditLog], int]:
        """Return a filtered, paginated page of audit rows and the total count."""
        conditions = []
        if admin_id is not None:
            conditions.append(AdminAuditLog.admin_id == admin_id)
        if action:
            conditions.append(AdminAuditLog.action == action)
        if entity:
            conditions.append(AdminAuditLog.entity == entity)
        if date_from is not None:
            conditions.append(AdminAuditLog.created_at >= date_from)
        if date_to is not None:
            conditions.append(AdminAuditLog.created_at <= date_to)

        page = max(1, page)
        limit = min(max(1, limit), 200)

        stmt = (
            select(AdminAuditLog)
            .where(*conditions)
            .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
            .offset((page - 1) * limit)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())

        count_stmt = select(func.count()).select_from(AdminAuditLog).where(*conditions)
        total = (await self.session.execute(count_stmt)).scalar_one()
        return rows, int(total)

    async def distinct_actions(self) -> list[str]:
        """Return the distinct actions present, for the filter dropdown."""
        result = await self.session.execute(
            select(AdminAuditLog.action).distinct().order_by(AdminAuditLog.action)
        )
        return list(result.scalars().all())
