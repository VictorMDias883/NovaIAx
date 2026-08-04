"""
Pydantic request/response schemas for the objectives endpoints.

These schemas define the shape of the JSON payloads accepted and
returned by the ``/objectives`` routes in
:mod:`app.api.v1.objective_router`.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class RegisterObjectiveRequest(BaseModel):
    """Request body for the ``POST /objectives/register`` endpoint.

    Attributes:
        title: Short title for the objective (2–255 characters).
        description: Optional longer description (up to 1000 characters).
        due_date: Deadline for the objective.  Must be a valid ISO-8601
            datetime string.
    """

    title: str = Field(min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    due_date: datetime


class ObjectiveResponse(BaseModel):
    """Response body returned by ``POST /objectives/register``.

    Attributes:
        id: The database-generated objective ID.
        title: The objective's title.
        description: The objective's description (may be ``None``).
        due_date: The objective's deadline.
        user_id: The ID of the user who owns this objective.
    """

    id: int
    title: str
    description: str | None
    due_date: datetime
    user_id: int
