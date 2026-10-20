"""
ORM model for the ``ai_feedback`` table.

The product must let a user report an offensive or wrong AI answer.  Each row
records the ``message_id`` the client received (the agent chat response
carries one) together with a coarse ``reason`` and optional free-form
``details``.  Operators review the queue in the admin console and resolve or
ignore each report via ``status``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: Lifecycle of a feedback review, driven by the admin console.
AI_FEEDBACK_STATUS_PENDING = "pending"
AI_FEEDBACK_STATUS_RESOLVED = "resolved"
AI_FEEDBACK_STATUS_IGNORED = "ignored"

#: Valid review statuses (kept as a plain tuple; the column is a free string).
AI_FEEDBACK_STATUSES = (
    AI_FEEDBACK_STATUS_PENDING,
    AI_FEEDBACK_STATUS_RESOLVED,
    AI_FEEDBACK_STATUS_IGNORED,
)


class AIFeedback(Base):
    """A user report about one AI answer.

    Table: ``ai_feedback``
    """

    __tablename__ = "ai_feedback"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The offending answer text, but only when the reporter chose to attach
    #: it to the report.  The backend never persists chat content on its own,
    #: so this column is populated explicitly by the client on submission and
    #: is the only reason the admin console may display an AI answer.
    message_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AI_FEEDBACK_STATUS_PENDING,
        server_default=AI_FEEDBACK_STATUS_PENDING,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
