"""
SQLAlchemy ORM models for the personal-finance domain.

Owner-scoped, UTC datetimes, native enums by member name.  **All amounts are
stored as integer cents** — never floats — so sums are exact and the API can
present a decimal amount without binary rounding drift.

* :class:`Expense` / :class:`Income` — dated one-off money movements.
* :class:`Bill` — a payable with a due date and a status.
* :class:`Budget` — a monthly per-category ceiling (``month`` = ``YYYY-MM``).

The backend only *records and aggregates* the user's own numbers; it never
gives investment, tax or legal advice (see the service layer's disclaimer).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class BillStatus(str, Enum):
    """Lifecycle of a bill."""

    PENDING = "pending"
    PAID = "paid"
    OVERDUE = "overdue"


class BillRecurrence(str, Enum):
    """How often a bill repeats."""

    NONE = "none"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class Expense(Base):
    """A single expense. Table: ``expenses``."""

    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Income(Base):
    """A single income. Table: ``incomes``."""

    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Bill(Base):
    """A payable with a due date. Table: ``bills``."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[BillStatus] = mapped_column(
        SQLEnum(BillStatus, name="bill_status", native_enum=True),
        nullable=False,
        default=BillStatus.PENDING,
        server_default=BillStatus.PENDING.name,
        index=True,
    )
    recurrence: Mapped[BillRecurrence] = mapped_column(
        SQLEnum(BillRecurrence, name="bill_recurrence", native_enum=True),
        nullable=False,
        default=BillRecurrence.NONE,
        server_default=BillRecurrence.NONE.name,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Budget(Base):
    """A monthly per-category ceiling. Table: ``budgets``.

    ``month`` is a ``YYYY-MM`` string; the pair ``(user_id, month, category)``
    is unique so a month's category budget can be upserted.
    """

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "month", "category", name="uq_budgets_user_id_month_category"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["Bill", "BillRecurrence", "BillStatus", "Budget", "Expense", "Income"]
