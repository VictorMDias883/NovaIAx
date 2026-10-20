"""
SQLAlchemy ORM model for the admin console's audit trail.

Table: ``admin_audit_log``.

Every state-changing action performed from the panel writes exactly one row
here (see :class:`app.services.admin_audit_service.AdminAuditService`).  The
``before``/``after`` snapshots are JSON objects that have already been
redacted of secrets and personal content by the caller — the audit trail is
metadata about a change, never a copy of user data.

``admin_id`` is nullable and uses ``ON DELETE SET NULL`` so deleting an admin
account never erases the history of what that account did; ``admin_email`` is
kept as a denormalised snapshot for the same reason.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AdminAuditLog(Base):
    """One recorded administrative action.

    Table: ``admin_audit_log``.
    """

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    admin_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


__all__ = ["AdminAuditLog"]
