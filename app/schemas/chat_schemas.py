"""
Pydantic request and response schemas for AI chat completion.
"""

from pydantic import BaseModel, Field


class ChatCompletionRequest(BaseModel):
    """Request body for the AI chat completion endpoint."""

    agent_id: int = Field(gt=0)
    user_message: str = Field(min_length=1, max_length=5000)


class ChatCompletionResponse(BaseModel):
    """Response body returned by the AI chat completion endpoint.

    ``assistant_message`` is the answer with the personalization marker
    stripped.  ``why`` (short reasons) and ``sources`` (the memory keys /
    group / sub-agent slugs that influenced the reply) come from the model's
    ``[personalization]`` marker and default to empty when the model did not
    emit one.
    """

    assistant_message: str
    message_id: str
    why: list[str] = []
    sources: list[str] = []


class ConversationClearResponse(BaseModel):
    """Response body returned after clearing a chat conversation.

    Attributes:
        agent_id: The agent whose conversation was cleared.
        cleared: Always ``True``; a missing agent yields ``404`` instead.
    """

    agent_id: int
    cleared: bool = True
