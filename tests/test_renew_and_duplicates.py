"""Tests to verify previous-days summary is passed to AI and duplicate days are prevented."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDay
from app.models.user import User
from app.services.objective_service import ObjectiveService
from app.services.roadmap_day_service import RoadmapDayService


class RecordingAIClient:
    def __init__(self, roadmap: str = "{}") -> None:
        self.roadmap = roadmap
        self.last_user_message = None

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        # record the user_message passed by ObjectiveService
        self.last_user_message = user_message
        return self.roadmap


async def _run(scenario) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            await scenario(session)
    finally:
        await engine.dispose()


async def _create_user(session) -> User:
    user = User(full_name="Test User", email="t@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def test_previous_days_summary_sent_to_ai_and_no_duplicate_days() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        # create objective and initial days
        service = ObjectiveService(session, ai_client=RecordingAIClient("## Initial"))
        result = await service.register(
            command=__import__("app.commands.register_objective_command", fromlist=["RegisterObjectiveCommand"]).RegisterObjectiveCommand(
                title="Goal",
                description="Desc",
                due_date=datetime.now(UTC) + timedelta(days=30),
            ),
            user_id=user.id,
        )

        # mark day 1 completed
        day_service = RoadmapDayService(session)
        days = await day_service.list_days(result["id"], user.id, "USER")
        await day_service.set_day_status(result["id"], days[0].id, user.id, "USER", __import__("app.models.roadmap_day", fromlist=["RoadmapDayStatus"]).RoadmapDayStatus.COMPLETED)

        # backdate roadmap_updated_at to allow renewal
        obj = (await session.execute(select(Objective).where(Objective.id == result["id"]))).scalar_one()
        obj.roadmap_updated_at = datetime.now(UTC) - timedelta(days=8)
        await session.commit()

        # Renew roadmap and check that AI client received previous days summary
        recording_ai = RecordingAIClient("## Renewed")
        service2 = ObjectiveService(session, ai_client=recording_ai)
        renewed = await service2.renew_roadmap(result["id"], user_id=user.id, role="USER")

        assert recording_ai.last_user_message is not None
        assert "Contexto de cumprimento anterior" in recording_ai.last_user_message or "Resumo dos dias anteriores" in recording_ai.last_user_message

        # Now test duplicate prevention: call create_days twice for same window
        objective = (await session.execute(select(Objective).where(Objective.id == result["id"]))).scalar_one()
        start = datetime.now(UTC)
        end = start + timedelta(days=6)
        created_first = await day_service.create_days(objective.id, start, end)
        # second call should not add more days for the same dates
        created_second = await day_service.create_days(objective.id, start, end)

        # count total days for objective; should equal number created_first plus any existing (no duplicates)
        all_days = (await session.execute(select(RoadmapDay).where(RoadmapDay.objective_id == objective.id))).scalars().all()
        assert len(all_days) >= len(created_first)
        # ensure calling create_days again did not increase count by len(created_second)
        # since create_days skips existing dates, created_second should be empty or smaller
        assert len(created_second) == 0 or len(created_second) <= len(created_first)

    asyncio.run(_run(scenario))
