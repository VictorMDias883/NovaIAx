"""
ORM model for the ``user_agents`` table.

Personalization profile of one user's assistant: the agent's name and tone,
the preferred language/timezone, the audience groups and sub-agents that are
active, a simplification toggle and a learning toggle that controls whether
explicit user statements may be turned into auto-learned memories.

Only one profile exists per user (``user_id`` is unique): the row is created
lazily with defaults the first time any personalization endpoint or a
personalized conversation is used.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONList


class UserAgent(Base):
    """ORM model representing a user's personalization profile.

    Table: ``user_agents``

    Attributes:
        user_id: Unique per-user primary key (FK to ``users.id``).
        agent_name: Display name of the assistant (2–30 chars, default
            ``"MeuAgente"``).
        tone: Free-form tone descriptor (e.g. "amigável e direto").
        language: Preferred reply language (default ``"pt-BR"``).
        timezone: IANA timezone (default ``"America/Sao_Paulo"``).
        active_group_slugs: Slugs of the activated audience groups.
        active_sub_agent_slugs: Slugs of the activated sub-agents.
        simplified_mode: When ``True`` the assistant gives shorter answers.
        learning_enabled: When ``True`` explicit user statements may be
            auto-learned into :class:`~app.models.user_memory.UserMemory`
            rows (toggled by ``PUT /me/learning``).
        created_at/updated_at: Audit timestamps.
    """

    __tablename__ = "user_agents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(30), nullable=False, default="MeuAgente")
    tone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="pt-BR")
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="America/Sao_Paulo")
    active_group_slugs: Mapped[list[str]] = mapped_column(JSONList, nullable=False, default=list)
    active_sub_agent_slugs: Mapped[list[str]] = mapped_column(JSONList, nullable=False, default=list)
    simplified_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    learning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
