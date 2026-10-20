"""
Service layer for the admin audit trail.

Every state-changing action taken from the operator console funnels through
:meth:`AdminAuditService.record`.  The before/after snapshots are redacted
(:func:`app.utils.redaction.redact`) before they touch the database, so the
trail stores metadata about a change, never secrets or personal content.

The write is committed on its own so an audit row survives even if the
caller's request ends in a redirect or an unhandled error.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.admin_audit_log_repository import AdminAuditLogRepository
from app.utils.redaction import redact


class AdminAuditService:
    """Append-only audit log for administrator actions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AdminAuditLogRepository(session)

    async def record(
        self,
        *,
        admin: dict[str, Any],
        action: str,
        entity: str | None = None,
        entity_id: int | str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Persist one redacted audit entry.

        Args:
            admin: The acting admin's identity (needs ``id``/``email``).
            action: A stable verb, e.g. ``"user.promote"``.
            entity: The kind of object acted on, e.g. ``"user"``.
            entity_id: The object's identifier, stored as text.
            before: Pre-change snapshot (redacted before storage).
            after: Post-change snapshot (redacted before storage).
            ip_address: Best-effort source IP.
        """
        raw_admin_id = admin.get("id")
        try:
            admin_id: int | None = int(str(raw_admin_id)) if raw_admin_id is not None else None
        except (TypeError, ValueError):
            admin_id = None

        await self.repo.add(
            admin_id=admin_id,
            admin_email=admin.get("email"),
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            before=redact(before) if before is not None else None,
            after=redact(after) if after is not None else None,
            ip_address=ip_address,
        )
        # Own commit so the trail is durable regardless of what the route does
        # next (redirect, exception, ...).
        await self.session.commit()


__all__ = ["AdminAuditService"]
