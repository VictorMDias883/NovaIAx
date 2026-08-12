"""
Pydantic request and response schemas for AI chat completion.
"""

from pydantic import BaseModel, Field


class ChatCompletionRequest(BaseModel):
    """Request body for the AI chat completion endpoint."""

    agent_id: int = Field(gt=0)
    user_message: str = Field(min_length=1, max_length=5000)


class ChatCompletionResponse(BaseModel):
    """Response body returned by the AI chat completion endpoint."""

    assistant_message: str
