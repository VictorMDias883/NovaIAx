"""
Pydantic request/response schemas for the objectives endpoints.

These schemas define the shape of the JSON payloads accepted and
returned by the ``/objectives`` routes in
:mod:`app.api.v1.objective_router`.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.roadmap_day_schemas import RoadmapDayResponse


class RegisterObjectiveRequest(BaseModel):
    """Request body for the ``POST /objectives/register`` endpoint.

    Attributes:
        title: Short title for the *general* objective (2–255
            characters), e.g. "learning English".
        description: Optional longer description (up to 1000 characters).
        due_date: Deadline for the objective.  Must be a valid ISO-8601
            datetime string.
    """

    title: str = Field(min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    due_date: datetime


class ObjectiveResponse(BaseModel):
    """Response body returned by ``POST /objectives/register``.

    The objective is the user's general goal.  The roadmap lives inside
    it as the ``roadmap`` markdown plus the per-day ``days`` records,
    each carrying its basic meta / minimum objective.

    Attributes:
        id: The database-generated objective ID.
        title: The objective's title.
        description: The objective's description (may be ``None``).
        roadmap: The AI-generated roadmap for the objective (may be
            ``None`` until generated).
        roadmap_updated_at: Timestamp of the last roadmap generation
            (may be ``None`` until generated).
        due_date: The objective's deadline.
        user_id: The ID of the user who owns this objective.
        days: The roadmap days of the objective, each with its basic
            meta / minimum objective.
    """

    id: int
    title: str
    description: str | None
    roadmap: str | None = None
    roadmap_updated_at: datetime | None = None
    due_date: datetime
    user_id: int
    days: list[RoadmapDayResponse] = []
