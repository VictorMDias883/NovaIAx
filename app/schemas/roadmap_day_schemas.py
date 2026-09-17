"""Pydantic schemas for the roadmap-days endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.roadmap_day import RoadmapDayStatus


class RoadmapDayResponse(BaseModel):
    """Response body for a single roadmap day."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    objective_id: int
    day_number: int
    day_date: datetime
    content: str | None = None
    status: RoadmapDayStatus
    completed_at: datetime | None


class RoadmapDayListResponse(BaseModel):
    """Response body for listing an objective's roadmap days."""

    objective_id: int
    days: list[RoadmapDayResponse]


class RoadmapDayStatusUpdate(BaseModel):
    """Request body for updating a roadmap day's status."""

    status: RoadmapDayStatus = Field(description="Novo status do dia do roadmap")
