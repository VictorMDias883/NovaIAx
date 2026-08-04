"""
SQLAlchemy ORM model for the ``objectives`` table.

This model represents a user's objective (a task or goal with a due date).
Each objective belongs to exactly one :class:`User`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ``TYPE_CHECKING`` guard prevents a circular import at runtime.
# The ``User`` model is only needed for type annotations.
if TYPE_CHECKING:
    from app.models.user import User

from app.db.session import Base


class Objective(Base):
    """ORM model representing a user's objective.

    Table: ``objectives``

    Attributes:
        id: Primary key (auto-incremented integer).
        title: Short title for the objective (max 255 characters).
        description: Optional longer description (max 1000 characters).
        due_date: Deadline for the objective.  Must be timezone-aware.
        created_at: Timestamp of when the objective was created.
            Defaults to ``datetime.utcnow()`` at the application level.
        user_id: Foreign key referencing ``users.id``.  Establishes
            the many-to-one relationship to :class:`User`.
        user: The owning :class:`User` instance (loaded via the
            ``back_populates`` relationship).
    """

    __tablename__ = "objectives"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # Many-to-one relationship: many objectives → one user.
    # ``back_populates="objectives"`` links this to the ``objectives``
    # relationship on the ``User`` model.
    user: Mapped["User"] = relationship(back_populates="objectives")
