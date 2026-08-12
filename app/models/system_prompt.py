"""
ORM model for the ``system_prompts`` table.

This model stores reusable system prompts that can be managed by
administrators via the API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SystemPrompt(Base):
    """ORM model representing a system prompt record."""

    __tablename__ = "system_prompts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tipo: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    system_prompt: Mapped[str] = mapped_column(String(5000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
