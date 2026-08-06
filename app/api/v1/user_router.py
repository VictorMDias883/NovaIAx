from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.commands.demote_user_command import DemoteUserCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.db.session import SessionLocal
from app.schemas.user_schemas import ActionResponse, UserListResponse
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


@router.patch("/{user_id}/promote", response_model=ActionResponse)
async def promote_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    service = UserService(session)
    result = await service.promote(current_user, PromoteUserCommand(target_id=user_id))
    return ActionResponse(**result)


@router.patch("/{user_id}/demote", response_model=ActionResponse)
async def demote_user(
    user_id: int,
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    service = UserService(session)
    result = await service.demote(current_user, DemoteUserCommand(target_id=user_id))
    return ActionResponse(**result)


@router.get("/", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    service = UserService(session)
    result = await service.list_users(page=page, limit=limit)
    return UserListResponse(**result)
