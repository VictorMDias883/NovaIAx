"""Pydantic schemas for the objective-assistant endpoint."""

from pydantic import BaseModel, Field

from app.schemas.objective_schemas import ObjectiveResponse


class ObjectiveAssistantRequest(BaseModel):
    """Request body for the objective-assistant endpoint."""

    user_message: str = Field(min_length=1, max_length=5000)


class ObjectiveAssistantResponse(BaseModel):
    """Response body returned by the objective-assistant endpoint.

    Attributes:
        assistant_message: The assistant's reply for this turn.  When the
            model returned a complete objective but its due date could not be
            parsed, this is a request for the user to confirm the date, and
            ``created_objective`` stays ``None``.
        created_objective: The objective that was auto-registered, or
            ``None`` when the assistant only asked a question (or the draft
            was not usable yet).
    """

    assistant_message: str
    created_objective: ObjectiveResponse | None = None
