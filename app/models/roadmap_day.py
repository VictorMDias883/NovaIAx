"""
SQLAlchemy ORM model for the ``roadmap_days`` table.

This model tracks each individual day of an objective's roadmap window
so the system can report which days the user has already fulfilled.
Each day belongs to exactly one :class:`Objective`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum as SQLEnum, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ``TYPE_CHECKING`` guard prevents a circular import at runtime.
# The ``Objective`` model is only needed for type annotations.
if TYPE_CHECKING:
    from app.models.objective import Objective

from app.db.session import Base


class RoadmapDayStatus(str, Enum):
    """Status of a single roadmap day."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"


class RoadmapDay(Base):
    """ORM model representing one day of an objective's roadmap.

    Table: ``roadmap_days``

    Attributes:
        id: Primary key (auto-incremented integer).
        objective_id: Foreign key referencing ``objectives.id``.
        day_number: Position of the day inside its 7-day window (1..N).
        day_date: Calendar date the day refers to (timezone-aware).
        status: Current :class:`RoadmapDayStatus` of the day.
        completed_at: Timestamp of when the day was marked as completed
            (``None`` until then).
        created_at: Timestamp of when the record was created.
        objective: The owning :class:`Objective` instance (loaded via
            the ``back_populates`` relationship).
    """

    __tablename__ = "roadmap_days"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    objective_id: Mapped[int] = mapped_column(ForeignKey("objectives.id"), nullable=False, index=True)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    day_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[RoadmapDayStatus] = mapped_column(
        SQLEnum(RoadmapDayStatus, name="roadmap_day_status", native_enum=True),
        nullable=False,
        server_default=RoadmapDayStatus.PENDING.value,
        index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Many-to-one relationship: many days → one objective.
    # ``back_populates="days"`` links this to the ``days`` relationship
    # on the ``Objective`` model.
    objective: Mapped["Objective"] = relationship(back_populates="days")
