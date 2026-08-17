"""Tests for the objective service, including AI-generated roadmaps."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.user import User
from app.services.objective_service import ObjectiveService


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


async def _create_user(session) -> User:
    user = User(full_name="Test User", email="test@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _register_objective(session, service, user_id: int) -> dict[str, object]:
    command = RegisterObjectiveCommand(
        title="Aprender FastAPI",
        description="Dominar o framework",
        due_date=datetime.now(UTC) + timedelta(days=30),
    )
    return await service.register(command, user_id=user_id)


async def _backdate_roadmap(session, objective_id: int, days: int) -> None:
    stmt = select(Objective).where(Objective.id == objective_id)
    objective = (await session.execute(stmt)).scalar_one()
    objective.roadmap_updated_at = datetime.now(UTC) - timedelta(days=days)
    await session.commit()


def test_register_generates_and_stores_roadmap() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("## Etapa 1\n- Estudar"))

        result = await _register_objective(session, service, user.id)

        assert result["title"] == "Aprender FastAPI"
        assert result["roadmap"] == "## Etapa 1\n- Estudar"
        assert result["roadmap_updated_at"] is not None
        assert result["user_id"] == user.id
        assert result["id"] is not None

    asyncio.run(_run(scenario))


def test_register_decomposes_objective_into_daily_metas() -> None:
    payload = (
        '{"days": ['
        '{"day": 1, "meta": "Aprender saudações e o alfabeto"},'
        '{"day": 2, "meta": "Estudar vocabulário básico de rotina"},'
        '{"day": 3, "meta": "Praticar escuta com vídeos curtos"}'
        "]}"
    )

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await _register_objective(session, service, user.id)

        assert result["roadmap"] == (
            "## Roadmap\n"
            "- Dia 1: Aprender saudações e o alfabeto\n"
            "- Dia 2: Estudar vocabulário básico de rotina\n"
            "- Dia 3: Praticar escuta com vídeos curtos"
        )
        assert len(result["days"]) == 7
        assert result["days"][0]["day_number"] == 1
        assert result["days"][0]["content"] == "Aprender saudações e o alfabeto"
        assert result["days"][2]["content"] == "Praticar escuta com vídeos curtos"
        assert result["days"][6]["content"] is None

    asyncio.run(_run(scenario))


def test_register_rejects_past_due_date() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))

        command = RegisterObjectiveCommand(
            title="Meta antiga",
            description=None,
            due_date=datetime.now(UTC) - timedelta(days=1),
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.register(command, user_id=user.id)
        assert exc_info.value.status_code == 400

    asyncio.run(_run(scenario))


def test_renew_roadmap_updates_after_7_days() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("## Semana 2\n- Avançar"))
        result = await _register_objective(session, service, user.id)
        await _backdate_roadmap(session, result["id"], days=8)

        renewed = await service.renew_roadmap(
            objective_id=result["id"],
            user_id=user.id,
            role="USER",
        )

        assert renewed["roadmap"] == "## Semana 2\n- Avançar"
        assert renewed["id"] == result["id"]

    asyncio.run(_run(scenario))


def test_renew_roadmap_regenerates_daily_metas() -> None:
    payload = '{"days": [{"day": 1, "meta": "Revisar o vocabulário da semana 1"}, {"day": 2, "meta": "Treinar conversação"}]}'

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))
        result = await _register_objective(session, service, user.id)
        await _backdate_roadmap(session, result["id"], days=8)

        renewed = await service.renew_roadmap(
            objective_id=result["id"],
            user_id=user.id,
            role="USER",
        )

        assert renewed["roadmap"].startswith("## Roadmap")
        assert renewed["days"][0]["content"] == "Revisar o vocabulário da semana 1"
        assert renewed["days"][1]["content"] == "Treinar conversação"
        assert renewed["days"][0]["day_number"] == 1

    asyncio.run(_run(scenario))


def test_renew_roadmap_rejected_within_window() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))
        result = await _register_objective(session, service, user.id)

        with pytest.raises(HTTPException) as exc_info:
            await service.renew_roadmap(
                objective_id=result["id"],
                user_id=user.id,
                role="USER",
            )
        assert exc_info.value.status_code == 409

    asyncio.run(_run(scenario))


def test_renew_roadmap_denied_for_non_owner() -> None:
    async def scenario(session) -> None:
        owner = await _create_user(session)
        other = User(full_name="Other", email="other@example.com", password_hash="x")
        session.add(other)
        await session.commit()
        await session.refresh(other)

        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))
        result = await _register_objective(session, service, owner.id)

        with pytest.raises(HTTPException) as exc_info:
            await service.renew_roadmap(
                objective_id=result["id"],
                user_id=other.id,
                role="USER",
            )
        assert exc_info.value.status_code == 403

    asyncio.run(_run(scenario))


def test_renew_roadmap_not_found() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))

        with pytest.raises(HTTPException) as exc_info:
            await service.renew_roadmap(objective_id=999, user_id=user.id, role="USER")
        assert exc_info.value.status_code == 404

    asyncio.run(_run(scenario))
