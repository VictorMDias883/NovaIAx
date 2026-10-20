"""
Pydantic request/response schemas for system prompt administration.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SystemPromptRequest(BaseModel):
    """Request payload for creating or updating a system prompt."""

    tipo: str = Field(min_length=2, max_length=100)
    system_prompt: str = Field(min_length=1, max_length=5000)
    description: str = Field(default="", max_length=2000)
    icon: str = Field(default="🤖", max_length=50)
    min_plan: str = Field(default="free", max_length=20)
    audience: str = Field(default="", max_length=120)
    active: bool = True
    sort_order: int = 0


class SystemPromptMetadataMixin(BaseModel):
    """Catalog metadata shared by the admin and non-admin read shapes."""

    id: int
    tipo: str
    active: bool
    sort_order: int
    icon: str
    description: str
    min_plan: str
    audience: str
    created_at: datetime


class SystemPromptResponse(SystemPromptMetadataMixin):
    """Response payload for a system prompt record.

    Includes the prompt text, so it is only ever served to administrators.
    """

    system_prompt: str


class SystemPromptSummaryResponse(SystemPromptMetadataMixin):
    """Response payload for a system prompt served to a non-admin caller.

    The ``system_prompt`` text is deliberately absent: these endpoints are
    readable by any authenticated user, and the prompt is the agent's own
    instruction set rather than data the user is meant to read.
    """


class SystemPromptListResponse(BaseModel):
    """Response payload for listing system prompts (admin view)."""

    prompts: list[SystemPromptResponse]
    page: int
    limit: int
    total: int


class SystemPromptSummaryListResponse(BaseModel):
    """Response payload for listing system prompts (non-admin view)."""

    prompts: list[SystemPromptSummaryResponse]
    page: int
    limit: int
    total: int
