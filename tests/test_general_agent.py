"""Tests for the general agent service."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.commands.general_agent_command import GeneralAgentCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDayStatus
from app.models.user import User
from app.services.general_agent_service import GeneralAgentService
from app.services.roadmap_day_service import RoadmapDayService


class RecordingAIClient:
    def __init__(self, response: str = "Resposta do agente") -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.user_message: str | None = None

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        self.system_prompt = system_prompt
        self.user_message = user_message
        return self.response


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


async def _create_user(session) -> User:
    user = User(full_name="Test User", email="test@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _create_objective(session, user_id: int, title: str = "Aprender FastAPI") -> Objective:
    objective = Objective(
        title=title,
        description="Dominar o framework",
        due_date=datetime.now(UTC) + timedelta(days=30),
        user_id=user_id,
    )
    session.add(objective)
    await session.commit()
    await session.refresh(objective)
    return objective


def test_returns_ai_reply() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        ai_client = RecordingAIClient(response="Olá! Tudo certo por aqui.")
        service = GeneralAgentService(session, ai_client=ai_client)

        result = await service.create_completion(
            GeneralAgentCommand(user_message="Como estou indo?"),
            user_id=user.id,
        )

        assert result["assistant_message"] == "Olá! Tudo certo por aqui."

    asyncio.run(_run(scenario))


def test_context_includes_objective_and_day_statuses() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = await _create_objective(session, user.id)
        day_service = RoadmapDayService(session)
        days = await day_service.create_days(
            objective.id,
            datetime.now(UTC),
            datetime.now(UTC) + timedelta(days=2),
        )
        await day_service.set_day_status(
            objective_id=objective.id,
            day_id=days[0].id,
            user_id=user.id,
            role="USER",
            status=RoadmapDayStatus.COMPLETED,
        )

        ai_client = RecordingAIClient()
        service = GeneralAgentService(session, ai_client=ai_client)
        await service.create_completion(
            GeneralAgentCommand(user_message="Quais dias eu cumpri?"),
            user_id=user.id,
        )

        assert ai_client.user_message is not None
        assert "Aprender FastAPI" in ai_client.user_message
        assert "1 cumprido" in ai_client.user_message
        assert "COMPLETED" in ai_client.user_message
        assert "Dia 1" in ai_client.user_message

    asyncio.run(_run(scenario))


def test_context_without_objectives() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        ai_client = RecordingAIClient()
        service = GeneralAgentService(session, ai_client=ai_client)

        await service.create_completion(
            GeneralAgentCommand(user_message="Quantas metas eu tenho?"),
            user_id=user.id,
        )

        assert ai_client.user_message is not None
        assert "Nenhum objetivo cadastrado" in ai_client.user_message

    asyncio.run(_run(scenario))


def test_system_prompt_instructs_agent_role() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        ai_client = RecordingAIClient()
        service = GeneralAgentService(session, ai_client=ai_client)

        await service.create_completion(
            GeneralAgentCommand(user_message="Oi"),
            user_id=user.id,
        )

        assert ai_client.system_prompt is not None
        assert "Agente Geral" in ai_client.system_prompt
