"""
ORM model for the ``user_memories`` table.

Long-lived facts the assistant uses as personalization context.  Each memory
belongs to one user and has:

* a stable ``key`` (unique per user; the ``auto_`` prefix is reserved for
  auto-learned entries);
* the remembered ``value``;
* a ``source`` — ``"user"`` for memories the user explicitly saved via
  ``POST /me/memories``, ``"learned"`` for facts auto-learned from an
  explicit user statement during a conversation;
* an ``enabled`` flag, so a memory can be turned off without deleting it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class UserMemory(Base):
    """ORM model representing a long-lived user memory.

    Table: ``user_memories``
    """

    __tablename__ = "user_memories"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_memories_user_id_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
