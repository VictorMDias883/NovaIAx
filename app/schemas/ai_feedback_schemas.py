"""Request/response schemas for ``POST /ai/feedback``."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FeedbackReason(str, Enum):
    """Coarse reasons a user can attach to an AI answer report."""

    OFFENSIVE = "offensive"
    WRONG = "wrong"
    UNSAFE = "unsafe"
    OTHER = "other"


class AIFeedbackRequest(BaseModel):
    """Body for ``POST /ai/feedback``."""

    message_id: str = Field(min_length=1, max_length=64)
    reason: FeedbackReason
    details: str | None = Field(default=None, max_length=2000)


class AIFeedbackResponse(BaseModel):
    """Echo of the stored report."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: str
    reason: FeedbackReason
    created_at: datetime
