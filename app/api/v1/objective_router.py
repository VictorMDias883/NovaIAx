from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import SessionLocal
from app.schemas.objective_schemas import ObjectiveResponse, RegisterObjectiveRequest
from app.services.objective_service import ObjectiveService

router = APIRouter(prefix="/objectives", tags=["objectives"])


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@router.post("/register", response_model=ObjectiveResponse)
async def register_objective(
    payload: RegisterObjectiveRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ObjectiveResponse:
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
