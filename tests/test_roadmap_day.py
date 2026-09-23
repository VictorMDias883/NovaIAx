"""Tests for the roadmap-day tracking service and objective integration."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus
from app.models.user import User
from app.services.objective_service import ObjectiveService
from app.services.roadmap_day_service import RoadmapDayService
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class DummyAIClient:
    def __init__(self, roadmap: str) -> None:
        self.roadmap = roadmap

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.roadmap


async def _run(scenario) -> None:
    """Execute an async scenario against an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with session_factory() as session:
            await scenario(session)
    finally:
        await engine.dispose()


async def _create_user(session, email: str = "test@example.com") -> User:
    user = User(full_name="Test User", email=email, password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _create_objective(session, user_id: int) -> Objective:
    objective = Objective(
        title="Aprender FastAPI",
        description="Dominar o framework",
        due_date=datetime.now(UTC) + timedelta(days=30),
        user_id=user_id,
    )
    session.add(objective)
    await session.commit()
    await session.refresh(objective)
    return objective


def test_create_days_covers_window_inclusive() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)
        start = datetime.now(UTC)
        end = start + timedelta(days=6)

        days = await service.create_days(objective.id, start, end)

        assert len(days) == 7
        assert days[0].day_number == 1
        assert days[6].day_number == 7
        assert days[0].day_date.date() == start.date()
        assert days[6].day_date.date() == end.date()
        assert all(day.status == RoadmapDayStatus.PENDING for day in days)

    asyncio.run(_run(scenario))


def test_create_days_single_day_window() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)
        now = datetime.now(UTC)

        days = await service.create_days(objective.id, now, now)

        assert len(days) == 1
        assert days[0].day_number == 1

    asyncio.run(_run(scenario))


def test_create_days_stores_per_day_contents() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)

        days = await service.create_days(
            objective.id,
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(days=2),
            contents={
                1: "Aprender saudações básicas",
                2: "Praticar vocabulário de rotina",
                3: "Assistir um vídeo em inglês",
            },
        )

        assert [day.content for day in days] == [
            "Aprender saudações básicas",
            "Praticar vocabulário de rotina",
            "Assistir um vídeo em inglês",
        ]

    asyncio.run(_run(scenario))


def test_set_day_status_marks_completed() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)
        days = await service.create_days(objective.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=6))

        updated = await service.set_day_status(
            objective_id=objective.id,
            day_id=days[1].id,
            user_id=user.id,
            role="USER",
            status=RoadmapDayStatus.COMPLETED,
        )

        assert updated.status == RoadmapDayStatus.COMPLETED
        assert updated.completed_at is not None

    asyncio.run(_run(scenario))


def test_set_day_status_back_to_pending_clears_completed_at() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)
        days = await service.create_days(objective.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=6))

        await service.set_day_status(
            objective_id=objective.id,
            day_id=days[0].id,
            user_id=user.id,
            role="USER",
            status=RoadmapDayStatus.COMPLETED,
        )
        updated = await service.set_day_status(
            objective_id=objective.id,
            day_id=days[0].id,
            user_id=user.id,
            role="USER",
            status=RoadmapDayStatus.PENDING,
        )

        assert updated.status == RoadmapDayStatus.PENDING
        assert updated.completed_at is None

    asyncio.run(_run(scenario))


def test_set_day_status_denied_for_non_owner() -> None:
    async def scenario(session) -> None:
        owner = await _create_user(session, email="owner@example.com")
        other = await _create_user(session, email="other@example.com")
        objective = await _create_objective(session, owner.id)
        service = RoadmapDayService(session)
        days = await service.create_days(objective.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=6))

        with pytest.raises(HTTPException) as exc_info:
            await service.set_day_status(
                objective_id=objective.id,
                day_id=days[0].id,
                user_id=other.id,
                role="USER",
                status=RoadmapDayStatus.COMPLETED,
            )
        assert exc_info.value.status_code == 403

    asyncio.run(_run(scenario))


def test_set_day_status_not_found() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)

        with pytest.raises(HTTPException) as exc_info:
            await service.set_day_status(
                objective_id=objective.id,
                day_id=999,
                user_id=user.id,
                role="USER",
                status=RoadmapDayStatus.COMPLETED,
            )
        assert exc_info.value.status_code == 404

    asyncio.run(_run(scenario))


def test_set_day_status_objective_not_found() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = RoadmapDayService(session)

        with pytest.raises(HTTPException) as exc_info:
            await service.set_day_status(
                objective_id=999,
                day_id=1,
                user_id=user.id,
                role="USER",
                status=RoadmapDayStatus.COMPLETED,
            )
        assert exc_info.value.status_code == 404

    asyncio.run(_run(scenario))


def test_register_objective_creates_roadmap_days() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("## Etapa 1\n- Estudar"))
        command = RegisterObjectiveCommand(
            title="Aprender FastAPI",
            description="Dominar o framework",
            due_date=datetime.now(UTC) + timedelta(days=30),
        )

        result = await service.register(command, user_id=user.id)

        days = (
            await session.execute(
                select(RoadmapDay).where(RoadmapDay.objective_id == result["id"])
            )
        ).scalars().all()
        assert len(days) == 7
        assert [day.day_number for day in days] == [1, 2, 3, 4, 5, 6, 7]

    asyncio.run(_run(scenario))


def test_list_days_returns_days_for_owner() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        service = RoadmapDayService(session)
        await service.create_days(objective.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=3))

        days = await service.list_days(objective.id, user.id, "USER")

        assert len(days) == 4

    asyncio.run(_run(scenario))
