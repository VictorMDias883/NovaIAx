from sqlalchemy.ext.asyncio import AsyncSession

from app.models.objective import Objective


class ObjectiveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, title: str, description: str | None, due_date, user_id: int) -> Objective:
        objective = Objective(title=title, description=description, due_date=due_date, user_id=user_id)
        self.session.add(objective)
        await self.session.commit()
        await self.session.refresh(objective)
        return objective
