"""
Pydantic request/response schemas for system prompt administration.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SystemPromptRequest(BaseModel):
    """Request payload for creating or updating a system prompt."""

    tipo: str = Field(min_length=2, max_length=100)
    system_prompt: str = Field(min_length=1, max_length=5000)


class SystemPromptResponse(BaseModel):
    """Response payload for a system prompt record."""

    id: int
    tipo: str
    system_prompt: str
    created_at: datetime


class SystemPromptListResponse(BaseModel):
    """Response payload for listing system prompts."""

    prompts: list[SystemPromptResponse]
    page: int
    limit: int
    total: int
