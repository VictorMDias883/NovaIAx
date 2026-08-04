from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.register_objective_command import RegisterObjectiveCommand
from app.repositories.objective_repository import ObjectiveRepository


class ObjectiveService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, command: RegisterObjectiveCommand, user_id: int) -> dict[str, object]:
        if command.due_date < datetime.utcnow():
            raise HTTPException(status_code=400, detail="due_date cannot be in the past")

        repo = ObjectiveRepository(self.session)
        objective = await repo.create(
            title=command.title,
            description=command.description,
            due_date=command.due_date,
            user_id=user_id,
        )
        return {
            "id": objective.id,
            "title": objective.title,
            "description": objective.description,
            "due_date": objective.due_date,
            "user_id": objective.user_id,
        }
