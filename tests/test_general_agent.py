"""Tests for the general agent service."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.commands.general_agent_command import GeneralAgentCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDayStatus
from app.models.user import User
from app.services.general_agent_service import GeneralAgentService
from app.services.roadmap_day_service import RoadmapDayService
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import make_conversation_cache


class RecordingAIClient:
    def __init__(self, response: str = "Resposta do agente") -> None:
        self.response = response
        self.system_prompt: str | None = None
        self.messages: list[dict[str, str]] | None = None

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.response

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        self.system_prompt = system_prompt
        self.messages = messages
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
        service = GeneralAgentService(
            session,
            ai_client=ai_client,
            conversation_cache=make_conversation_cache(),
        )

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
        service = GeneralAgentService(
            session,
            ai_client=ai_client,
            conversation_cache=make_conversation_cache(),
        )
        await service.create_completion(
            GeneralAgentCommand(user_message="Quais dias eu cumpri?"),
            user_id=user.id,
        )

        assert ai_client.messages is not None
        last_message = ai_client.messages[-1]
        assert last_message["role"] == "user"
        assert "Aprender FastAPI" in last_message["content"]
        assert "1 cumprido" in last_message["content"]
        assert "COMPLETED" in last_message["content"]
        assert "Dia 1" in last_message["content"]

    asyncio.run(_run(scenario))


def test_context_without_objectives() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        ai_client = RecordingAIClient()
        service = GeneralAgentService(
            session,
            ai_client=ai_client,
            conversation_cache=make_conversation_cache(),
        )

        await service.create_completion(
            GeneralAgentCommand(user_message="Quantas metas eu tenho?"),
            user_id=user.id,
        )

        assert ai_client.messages is not None
        assert "Nenhum objetivo cadastrado" in ai_client.messages[-1]["content"]

    asyncio.run(_run(scenario))


def test_system_prompt_instructs_agent_role() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        ai_client = RecordingAIClient()
        service = GeneralAgentService(
            session,
            ai_client=ai_client,
            conversation_cache=make_conversation_cache(),
        )

        await service.create_completion(
            GeneralAgentCommand(user_message="Oi"),
            user_id=user.id,
        )

        assert ai_client.system_prompt is not None
        assert "Agente Geral" in ai_client.system_prompt

    asyncio.run(_run(scenario))


def test_keeps_history_and_sends_it_on_next_turn() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        cache = make_conversation_cache()
        first = RecordingAIClient(response="Primeira resposta")
        service = GeneralAgentService(session, ai_client=first, conversation_cache=cache)
        await service.create_completion(
            GeneralAgentCommand(user_message="Olá"),
            user_id=user.id,
        )

        stored = await cache.get_messages(GeneralAgentService.AGENT_KEY, user.id)
        assert stored == [
            {"role": "user", "content": "Olá"},
            {"role": "assistant", "content": "Primeira resposta"},
        ]

        second = RecordingAIClient(response="Segunda resposta")
        service2 = GeneralAgentService(session, ai_client=second, conversation_cache=cache)
        await service2.create_completion(
            GeneralAgentCommand(user_message="Continuando"),
            user_id=user.id,
        )

        assert second.messages is not None
        assert second.messages[0] == {"role": "user", "content": "Olá"}
        assert second.messages[1] == {"role": "assistant", "content": "Primeira resposta"}
        assert second.messages[2]["role"] == "user"
        assert second.messages[2]["content"].startswith("Continuando")

    asyncio.run(_run(scenario))


def test_clear_conversation_removes_cached_messages() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        cache = make_conversation_cache()
        service = GeneralAgentService(
            session,
            ai_client=RecordingAIClient(response="Resposta"),
            conversation_cache=cache,
        )
        await service.create_completion(
            GeneralAgentCommand(user_message="Olá"),
            user_id=user.id,
        )

        assert await cache.get_messages(GeneralAgentService.AGENT_KEY, user.id) != []

        await cache.clear(GeneralAgentService.AGENT_KEY, user.id)

        assert await cache.get_messages(GeneralAgentService.AGENT_KEY, user.id) == []

    asyncio.run(_run(scenario))


def test_clear_conversation_route_is_registered() -> None:
    from app.api.v1.general_agent_router import router

    paths = {getattr(route, "path", None) for route in router.routes}
    assert "/agents/general/conversation" in paths
