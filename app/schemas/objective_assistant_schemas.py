"""Pydantic schemas for the objective-assistant endpoint."""

from pydantic import BaseModel, Field


class ObjectiveAssistantRequest(BaseModel):
    """Request body for the objective-assistant endpoint."""

    user_message: str = Field(min_length=1, max_length=5000)


class ObjectiveAssistantResponse(BaseModel):
    """Response body returned by the objective-assistant endpoint."""

    assistant_message: str
