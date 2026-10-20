"""
SQLAlchemy ORM models for the medication-care domain.

Owner-scoped (one ``user_id`` per row, indexed), UTC datetimes, native enums
stored by *member name* with lower-case *values* surfaced to the API — the
same conventions used across the schema.

* :class:`Medication` — a medication the user takes, with dosage/frequency and
  an optional daily schedule (``schedule_times`` as local ``"HH:MM"`` strings)
  bounded by ``start_date`` / ``end_date``.
* :class:`MedicationIntake` — one scheduled/taken/skipped intake, from which
  adherence is computed on read.

The AI never changes a dose: these tables only *record what the user already
takes* and whether an intake was marked taken.  No dose is ever computed or
altered by the backend.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONList


class IntakeStatus(str, Enum):
    """Lifecycle of a single medication intake."""

    PENDING = "pending"
    TAKEN = "taken"
    SKIPPED = "skipped"


class Medication(Base):
    """A medication registered by the user.

    Table: ``medications``
    """

    __tablename__ = "medications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dosage: Mapped[str | None] = mapped_column(String(120), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Local times of day ("HH:MM") at which the dose is due.  Interpreted in
    #: the user's ``/me/agent`` timezone by the app; the backend only stores
    #: the strings and never computes a dose.
    schedule_times: Mapped[list[str]] = mapped_column(JSONList, nullable=False, default=list)
    #: First day the schedule applies (``None`` for legacy/one-off records).
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: Last day the schedule applies (``None`` = open-ended).
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MedicationIntake(Base):
    """One scheduled/taken/skipped intake of a medication.

    Table: ``medication_intakes``
    """

    __tablename__ = "medication_intakes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    medication_id: Mapped[int] = mapped_column(
        ForeignKey("medications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[IntakeStatus] = mapped_column(
        SQLEnum(IntakeStatus, name="intake_status", native_enum=True),
        nullable=False,
        default=IntakeStatus.TAKEN,
        server_default=IntakeStatus.TAKEN.name,
        index=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["IntakeStatus", "Medication", "MedicationIntake"]
