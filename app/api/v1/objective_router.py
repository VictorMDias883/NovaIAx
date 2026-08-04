"""
API v1 objective endpoints.

This router provides endpoints for creating and managing user
objectives (tasks/goals with due dates).  All endpoints require
authentication — the :func:`get_current_user` dependency enforces
this at the route level.

Endpoints:
    - POST /objectives/register — Create a new objective for the
      authenticated user.

Architecture:
    Router → Command → Service → Repository → Database
"""

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import SessionLocal
from app.schemas.objective_schemas import ObjectiveResponse, RegisterObjectiveRequest
from app.services.objective_service import ObjectiveService

# Create a sub-router with the ``/objectives`` prefix and ``objectives`` tag.
router = APIRouter(prefix="/objectives", tags=["objectives"])


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    Uses an async context manager to ensure the session is properly
    closed after the request completes.

    Yields:
        An open :class:`AsyncSession` instance.
    """
    async with SessionLocal() as session:
        yield session


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
    # Extract a single session from the async generator dependency.
    async for db_session in get_session():
        break
    service = ObjectiveService(db_session)
    command = RegisterObjectiveCommand(
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
    )
    result = await service.register(command, user_id=int(current_user["id"]))
    return ObjectiveResponse(**result)
