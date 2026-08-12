"""
API v1 roadmap-days endpoints.

These endpoints allow the owner (or an administrator) of an objective
to list its roadmap days and to mark each day as fulfilled, skipped, or
pending again. The general agent uses this same data to report the
user's progress.

Endpoints:
    - GET   /objectives/{objective_id}/roadmap/days            — List days.
    - PATCH /objectives/{objective_id}/roadmap/days/{day_id}   — Update status.
"""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session
from app.schemas.roadmap_day_schemas import (
    RoadmapDayListResponse,
    RoadmapDayResponse,
    RoadmapDayStatusUpdate,
)
from app.services.roadmap_day_service import RoadmapDayService

router = APIRouter(prefix="/objectives", tags=["objectives"])


@router.get("/{objective_id}/roadmap/days", response_model=RoadmapDayListResponse)
async def list_roadmap_days(
    objective_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RoadmapDayListResponse:
    """List all roadmap days of an objective (owner or admin only)."""
    service = RoadmapDayService(session)
    days = await service.list_days(
        objective_id=objective_id,
        user_id=int(current_user["id"]),
        role=str(current_user.get("role", "")),
    )
    return RoadmapDayListResponse(objective_id=objective_id, days=days)


@router.patch("/{objective_id}/roadmap/days/{day_id}", response_model=RoadmapDayResponse)
async def update_roadmap_day_status(
    payload: RoadmapDayStatusUpdate,
    objective_id: int = Path(..., ge=1),
    day_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RoadmapDayResponse:
    """Update the status (e.g. mark as fulfilled) of a roadmap day."""
    service = RoadmapDayService(session)
    day = await service.set_day_status(
        objective_id=objective_id,
        day_id=day_id,
        user_id=int(current_user["id"]),
        role=str(current_user.get("role", "")),
        status=payload.status,
    )
    return RoadmapDayResponse(
        id=day.id,
        objective_id=day.objective_id,
        day_number=day.day_number,
        day_date=day.day_date,
        status=day.status,
        completed_at=day.completed_at,
    )
