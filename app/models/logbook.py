"""
SQLAlchemy ORM models for the trips / shifts / child-care logbook.

Owner-scoped, UTC datetimes.  A single :class:`LogEntry` table covers the three
requested log kinds (``trip``, ``shift``, ``childcare``) so one report routine
can aggregate them; ``metadata`` holds small kind-specific extras (e.g. the
child-care event type) as JSON, never as authoritative medical content.

Reports are computed on read by :mod:`app.services.logbook_service`; no report
is persisted, so a correction to an entry is reflected immediately.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONDict


class LogKind(str, Enum):
    """The three log kinds the product supports."""

    TRIP = "trip"
    SHIFT = "shift"
    CHILDCARE = "childcare"


class LogEntry(Base):
    """One dated log entry. Table: ``log_entries``.

    ``ended_at`` is optional: a trip/shift may be in progress.  ``duration``
    is derived (``ended_at - started_at``) by the service, never stored.
    """

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[LogKind] = mapped_column(
        SQLEnum(LogKind, name="log_kind", native_enum=True),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONDict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def duration_minutes(self) -> int | None:
        """Whole minutes between ``started_at`` and ``ended_at`` (or ``None``)."""
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at).total_seconds() // 60)


__all__ = ["LogEntry", "LogKind"]
