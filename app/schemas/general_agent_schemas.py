"""Pydantic schemas for the general agent endpoint."""

from pydantic import BaseModel, Field


class GeneralAgentRequest(BaseModel):
    """Request body for the general agent endpoint."""

    user_message: str = Field(min_length=1, max_length=5000)


class GeneralAgentResponse(BaseModel):
    """Response body returned by the general agent endpoint."""

    assistant_message: str


class GeneralAgentConversationClearResponse(BaseModel):
    """Response body returned when the conversation history is cleared."""

    message: str
