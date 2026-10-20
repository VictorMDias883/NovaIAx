"""
SQLAlchemy ORM models for the personal-productivity domain.

Everything here is **owner-scoped** (one ``user_id`` per row, indexed) and
time-based, so the whole set can be merged into a single chronological
``/me/agenda`` view.  The domain is deliberately small and self-contained:

* :class:`Task` and :class:`Reminder` — actionable items with an optional
  simple recurrence (``none|daily|weekly|monthly``).
* :class:`Note` — markdown text with tags and a pin flag.
* :class:`Checklist` / :class:`ChecklistItem` — reusable lists (compras, casa,
  bebê, viagem).
* :class:`Habit` / :class:`HabitCheckIn` — daily check-ins from which a streak
  is computed on read.
* :class:`FocusSession` — a pomodoro-style start/end block.

All datetimes are stored in UTC; the service layer converts to/from the
user's ``/me/agent`` timezone at the edges.  Native enums use uppercase
*member names* (SQLAlchemy's storage convention, matching the rest of the
schema) with lower-case *values* surfaced to the API.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.db.types import JSONList


class Recurrence(str, Enum):
    """Simple recurrence cadence shared by tasks and reminders."""

    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TaskPriority(str, Enum):
    """Relative importance of a task."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(str, Enum):
    """Lifecycle of a task."""

    PENDING = "pending"
    DONE = "done"
    CANCELED = "canceled"


class ReminderStatus(str, Enum):
    """Lifecycle of a reminder."""

    PENDING = "pending"
    DONE = "done"
    CANCELED = "canceled"


class ChecklistCategory(str, Enum):
    """The curated checklist buckets requested by the product."""

    COMPRAS = "compras"
    CASA = "casa"
    BEBE = "bebe"
    VIAGEM = "viagem"


class Task(Base):
    """A single actionable task owned by one user.

    Table: ``tasks``
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    priority: Mapped[TaskPriority] = mapped_column(
        SQLEnum(TaskPriority, name="task_priority", native_enum=True),
        nullable=False,
        default=TaskPriority.MEDIUM,
        server_default=TaskPriority.MEDIUM.name,
    )
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus, name="task_status", native_enum=True),
        nullable=False,
        default=TaskStatus.PENDING,
        server_default=TaskStatus.PENDING.name,
        index=True,
    )
    recurrence: Mapped[Recurrence] = mapped_column(
        SQLEnum(Recurrence, name="task_recurrence", native_enum=True),
        nullable=False,
        default=Recurrence.NONE,
        server_default=Recurrence.NONE.name,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Reminder(Base):
    """A time-anchored reminder owned by one user.

    Table: ``reminders``
    """

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    recurrence: Mapped[Recurrence] = mapped_column(
        SQLEnum(Recurrence, name="reminder_recurrence", native_enum=True),
        nullable=False,
        default=Recurrence.NONE,
        server_default=Recurrence.NONE.name,
    )
    status: Mapped[ReminderStatus] = mapped_column(
        SQLEnum(ReminderStatus, name="reminder_status", native_enum=True),
        nullable=False,
        default=ReminderStatus.PENDING,
        server_default=ReminderStatus.PENDING.name,
        index=True,
    )
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Note(Base):
    """A markdown note with tags and a pin flag.

    Table: ``notes``
    """

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSONList, nullable=False, default=list)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Checklist(Base):
    """A category-tagged checklist owned by one user.

    Table: ``checklists``
    """

    __tablename__ = "checklists"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[ChecklistCategory] = mapped_column(
        SQLEnum(ChecklistCategory, name="checklist_category", native_enum=True),
        nullable=False,
        default=ChecklistCategory.COMPRAS,
        server_default=ChecklistCategory.COMPRAS.name,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[ChecklistItem]] = relationship(
        back_populates="checklist", cascade="all, delete-orphan", lazy="selectin"
    )


class ChecklistItem(Base):
    """One line of a :class:`Checklist`.

    Table: ``checklist_items``
    """

    __tablename__ = "checklist_items"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    checklist_id: Mapped[int] = mapped_column(
        ForeignKey("checklists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(300), nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    checklist: Mapped[Checklist] = relationship(back_populates="items")


class Habit(Base):
    """A habit tracked through daily check-ins.

    Table: ``habits``
    """

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    check_ins: Mapped[list[HabitCheckIn]] = relationship(
        back_populates="habit", cascade="all, delete-orphan", lazy="selectin"
    )


class HabitCheckIn(Base):
    """A single daily habit check-in.

    Table: ``habit_check_ins`` — unique per ``(habit_id, check_date)`` so
    checking in twice on the same day is idempotent.
    """

    __tablename__ = "habit_check_ins"
    __table_args__ = (UniqueConstraint("habit_id", "check_date", name="uq_habit_check_ins_habit_id_check_date"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    habit_id: Mapped[int] = mapped_column(ForeignKey("habits.id", ondelete="CASCADE"), nullable=False, index=True)
    check_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    habit: Mapped[Habit] = relationship(back_populates="check_ins")


class FocusSession(Base):
    """A pomodoro-style focus block owned by one user.

    Table: ``focus_sessions``
    """

    __tablename__ = "focus_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="Foco")
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
