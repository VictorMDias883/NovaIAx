"""
SQLAlchemy ORM model for the ``users`` table.

This model represents an application user.  Each user can own multiple
:class:`Objective` records (see :mod:`app.models.objective`).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ``TYPE_CHECKING`` is used to avoid a circular import at runtime.
# The ``Objective`` model is only needed for type hints, not for
# actual execution, so we import it only during static type checking.
if TYPE_CHECKING:
    from app.models.objective import Objective

from app.db.session import Base


class User(Base):
    """ORM model representing an application user.

    Table: ``users``

    Attributes:
        id: Primary key (auto-incremented integer).
        full_name: The user's display name (max 255 characters).
        email: Unique email address (max 255 characters).  Used as the
            login identifier in the database-backed auth flow.
        password_hash: PBKDF2 hash of the user's password.  The raw
            password is never stored.
        created_at: Timestamp of when the user was created, set
            automatically by the database (``func.now()``).
        objectives: Relationship to the user's :class:`Objective`
            records.  ``cascade="all, delete-orphan"`` ensures that
            when a user is deleted, all their objectives are also
            deleted.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # One-to-many relationship: one user → many objectives.
    # ``back_populates`` links this to the ``user`` relationship on
    # the ``Objective`` model, keeping both sides in sync.
    objectives: Mapped[list["Objective"]] = relationship(back_populates="user", cascade="all, delete-orphan")
