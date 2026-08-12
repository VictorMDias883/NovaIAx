"""
API v1 objective endpoints.

This router provides endpoints for creating and managing user
objectives (tasks/goals with due dates).  All endpoints require
authentication — the :func:`get_current_user` dependency enforces
this at the route level.

Endpoints:
    - POST /objectives/register              — Create a new objective for
      the authenticated user (with an AI-generated 7-day roadmap).
    - POST /objectives/{id}/roadmap/renew    — Renew the objective's
      roadmap for the next 7-day window (available every 7 days).

Architecture:
    Router → Command → Service → Repository → Database
"""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session
from app.clients.ai_client import GroqAIClient
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.schemas.objective_schemas import ObjectiveResponse, RegisterObjectiveRequest
from app.services.objective_service import ObjectiveService

# Create a sub-router with the ``/objectives`` prefix and ``objectives`` tag.
router = APIRouter(prefix="/objectives", tags=["objectives"])


@router.post("/register", response_model=ObjectiveResponse)
async def register_objective(
    payload: RegisterObjectiveRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ObjectiveResponse:
    """Create a new objective for the authenticated user.

    The request body is validated by the :class:`RegisterObjectiveRequest`
    Pydantic model.  The :class:`ObjectiveService` then:
        1. Validates that the ``due_date`` is not in the past.
        2. Creates the objective record, associating it with the
           authenticated user.

    Args:
        payload: Validated request body with ``title``, ``description``,
            and ``due_date``.
        current_user: The authenticated user's identity (injected via
            :func:`get_current_user`).  Must contain an ``id`` key.
        session: Database session (injected via :func:`get_session`).

    Returns:
        An :class:`ObjectiveResponse` with the created objective's data.

    Raises:
        HTTPException(400): If ``due_date`` is in the past.
        HTTPException(401): If the user is not authenticated.
    """
    service = ObjectiveService(session, ai_client=GroqAIClient())
    command = RegisterObjectiveCommand(
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
    )
    result = await service.register(command, user_id=int(current_user["id"]))
    return ObjectiveResponse(**result)


@router.post("/{objective_id}/roadmap/renew", response_model=ObjectiveResponse)
async def renew_objective_roadmap(
    objective_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ObjectiveResponse:
    """Renew an objective's roadmap for the next 7-day window.

    The roadmap always covers only 7 days.  Once that window elapses,
    the owner (or an administrator) calls this endpoint to generate the
    next 7-day roadmap, using the previous roadmap as context.

    Args:
        objective_id: ID of the objective whose roadmap will be renewed.
        current_user: The authenticated user's identity (injected via
            :func:`get_current_user`).
        session: Database session (injected via :func:`get_session`).

    Returns:
        An :class:`ObjectiveResponse` with the updated roadmap.

    Raises:
        HTTPException(401): If the user is not authenticated.
        HTTPException(403): If the user does not own the objective and
            is not an administrator.
        HTTPException(404): If the objective does not exist.
        HTTPException(409): If the 7-day window has not elapsed yet.
    """
    service = ObjectiveService(session, ai_client=GroqAIClient())
    result = await service.renew_roadmap(
        objective_id=objective_id,
        user_id=int(current_user["id"]),
        role=str(current_user.get("role", "")),
    )
    return ObjectiveResponse(**result)
