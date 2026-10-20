"""Pydantic schemas for the general agent endpoint."""

from pydantic import BaseModel, Field


class GeneralAgentRequest(BaseModel):
    """Request body for the general agent endpoint."""

    user_message: str = Field(min_length=1, max_length=5000)


class GeneralAgentResponse(BaseModel):
    """Response body returned by the general agent endpoint.

    ``why`` (short reasons) and ``sources`` (the memory keys / group /
    sub-agent slugs that influenced the reply) come from the model's
    ``[personalization]`` marker and default to empty when the model did not
    emit one.
    """

    assistant_message: str
    message_id: str
    why: list[str] = []
    sources: list[str] = []


class GeneralAgentConversationClearResponse(BaseModel):
    """Response body returned when the conversation history is cleared."""

    message: str
