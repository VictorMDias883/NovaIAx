"""
ORM model recording the stable ``message_id`` of every chat turn.

Every chat endpoint (``/agents/chat``, ``/agents/general/chat``, ``/ai/chat``,
``/agents/device/command``) mints a stable ``message_id`` and persists a row
here.  This is what makes ``POST /ai/feedback`` safe: the repository can prove
that the ``message_id`` in a report belongs to the caller before storing it, so
a user cannot file a report against someone else's turn.

Only the identifier, the agent and the timestamp are stored — never the prompt
or the model's reply — so this table carries no user content.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ChatAgent(str, Enum):
    """Which chat surface produced a message id."""

    ORCHESTRATOR = "orchestrator"
    GENERAL = "general"
    AI = "ai"
    DEVICE = "device"


class ChatMessage(Base):
    """One persisted chat-turn identifier. Table: ``chat_messages``."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent: Mapped[ChatAgent] = mapped_column(
        SQLEnum(ChatAgent, name="chat_agent", native_enum=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = ["ChatAgent", "ChatMessage"]
