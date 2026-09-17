"""Failure-scenario tests for roadmap creation and navigation.

Covers the audit scenarios for the objective/roadmap flow:

1. Malformed AI responses (invalid JSON, empty ``days``, duplicate /
   out-of-order day numbers).
2. Objectives with no generated days (empty roadmap navigation).
3. User isolation between owners (no data leakage by ``user_id``).
4. Error handling (404/403) when an objective or day does not exist.
5. Multiple simultaneous roadmaps for the same user.
6. Renewal window ordering (day_number restarts each window).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.commands.general_agent_command import GeneralAgentCommand
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus
from app.models.user import User
from app.repositories.roadmap_day_repository import RoadmapDayRepository
from app.services.general_agent_service import GeneralAgentService
from app.services.objective_service import ObjectiveService
from app.services.roadmap_day_service import RoadmapDayService
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import make_conversation_cache


class DummyAIClient:
    def __init__(self, roadmap: str) -> None:
        self.roadmap = roadmap

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.roadmap

    async def create_chat_completion_with_history(self, system_prompt: str, messages: list[dict[str, str]]) -> str:
        return self.roadmap


class StubAIClient:
    """AI client that returns a canned reply to the general agent."""

    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.last_context: str | None = None

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.response

    async def create_chat_completion_with_history(self, system_prompt: str, messages: list[dict[str, str]]) -> str:
        self.last_context = messages[-1]["content"]
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


async def _create_user(session, email: str | None = None) -> User:
    user = User(
        full_name="Test User",
        email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _register(session, service, user_id: int, title: str = "Meta audit") -> dict[str, object]:
    command = RegisterObjectiveCommand(
        title=title,
        description="Audit",
        due_date=datetime.now(UTC) + timedelta(days=30),
    )
    return await service.register(command, user_id=user_id)


async def _backdate_roadmap(session, objective_id: int, days: int) -> None:
    objective = (
        await session.execute(select(Objective).where(Objective.id == objective_id))
    ).scalar_one()
    objective.roadmap_updated_at = datetime.now(UTC) - timedelta(days=days)
    await session.commit()


# ---------------------------------------------------------------------------
# Malformed AI response handling
# ---------------------------------------------------------------------------


def test_malformed_ai_json_is_stored_as_raw_roadmap_without_daily_metas() -> None:
    """A truncated/non-JSON AI reply must not crash and must leave day metas empty."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient('{"days": [{"day": 1'))

        result = await _register(session, service, user.id)

        assert result["roadmap"] == '{"days": [{"day": 1'
        assert result["id"] is not None
        assert len(result["days"]) == 7
        assert all(day["content"] is None for day in result["days"])
        # objective itself must still exist and be listed
        days = (
            await session.execute(select(RoadmapDay).where(RoadmapDay.objective_id == result["id"]))
        ).scalars().all()
        assert len(days) == 7

    asyncio.run(_run(scenario))


def test_empty_days_payload_falls_back_to_raw_roadmap() -> None:
    """``{"days": []}`` must not clear the roadmap; it falls back to raw text."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient('{"days": []}'))

        result = await _register(session, service, user.id)

        assert result["roadmap"] == '{"days": []}'
        assert all(day["content"] is None for day in result["days"])

    asyncio.run(_run(scenario))


def test_duplicate_day_numbers_last_one_wins() -> None:
    """Duplicate day numbers must not create duplicate day rows (last meta wins)."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        payload = '{"days": [{"day": 1, "meta": "a"}, {"day": 1, "meta": "b"}, {"day": 2, "meta": "c"}]}'
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await _register(session, service, user.id)

        assert len(result["days"]) == 7
        assert result["days"][0]["content"] == "b"
        assert result["days"][1]["content"] == "c"
        stored = (
            await session.execute(select(RoadmapDay).where(RoadmapDay.objective_id == result["id"]))
        ).scalars().all()
        assert len(stored) == 7
        assert len({d.day_number for d in stored}) == 7

    asyncio.run(_run(scenario))


def test_out_of_order_and_beyond_window_day_numbers() -> None:
    """Out-of-order days are mapped correctly; day numbers outside the window are dropped."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        payload = '{"days": [{"day": 3, "meta": "c"}, {"day": 1, "meta": "a"}, {"day": 2, "meta": "b"}, {"day": 10, "meta": "ignored"}]}'
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await _register(session, service, user.id)

        assert result["days"][0]["content"] == "a"
        assert result["days"][1]["content"] == "b"
        assert result["days"][2]["content"] == "c"
        assert result["days"][3]["content"] is None
        # NOTE: the roadmap markdown echoes every meta the AI returned
        # (including out-of-window day 10), while the day records cannot
        # represent it — an inconsistency to be aware of.
        assert "- Dia 10: ignored" in (result["roadmap"] or "")
        assert all(day["content"] != "ignored" for day in result["days"])

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# Objectives with no generated days (empty roadmap navigation)
# ---------------------------------------------------------------------------


def test_objective_with_no_days_is_listed_with_empty_days_and_context() -> None:
    """An objective that has no roadmap days is navigable everywhere."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = Objective(
            title="Sem dias",
            description="Nenhum dia gerado",
            due_date=datetime.now(UTC) + timedelta(days=10),
            user_id=user.id,
        )
        session.add(objective)
        await session.commit()
        await session.refresh(objective)

        service = ObjectiveService(session, ai_client=DummyAIClient("x"))
        listed = await service.list_by_user(user.id)
        assert len(listed) == 1
        assert listed[0]["days"] == []

        day_service = RoadmapDayService(session)
        days = await day_service.list_days(objective.id, user.id, "USER")
        assert days == []

        agent = GeneralAgentService(session, StubAIClient(), conversation_cache=make_conversation_cache())
        await agent.create_completion(
            GeneralAgentCommand(user_message="Quantos dias?"),
            user_id=user.id,
        )
        assert "nenhum registrado ainda" in (agent.ai_client.last_context or "")

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# Renewal windows: ordering and content refresh
# ---------------------------------------------------------------------------


def test_list_by_objective_orders_across_windows_by_date() -> None:
    """Renewed windows start day_number at 1 again; listing must not interleave."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        objective = Objective(
            title="Ordenação",
            description="Restart do day_number",
            due_date=datetime.now(UTC) + timedelta(days=60),
            user_id=user.id,
        )
        session.add(objective)
        await session.commit()
        await session.refresh(objective)

        day_service = RoadmapDayService(session)
        w1 = await day_service.create_days(
            objective.id,
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 7, tzinfo=UTC),
            contents={i: f"w1-d{i}" for i in range(1, 8)},
        )
        w2 = await day_service.create_days(
            objective.id,
            datetime(2026, 9, 8, tzinfo=UTC),
            datetime(2026, 9, 14, tzinfo=UTC),
            contents={i: f"w2-d{i}" for i in range(1, 8)},
        )
        assert len(w1) == 7 and len(w2) == 7

        repo = RoadmapDayRepository(session)
        all_days = await repo.list_by_objective(objective.id)

        dates = [d.day_date.date().isoformat() for d in all_days]
        assert dates == sorted(dates), f"days interleaved across windows: {dates}"
        # window order preserved: all of window 1 before any of window 2
        w1_ids = {d.id for d in w1}
        w2_seen_before_w1_done = False
        for d in all_days:
            if d.id in w1_ids:
                assert not w2_seen_before_w1_done
            elif d.id in {x.id for x in w2}:
                w2_seen_before_w1_done = True
        assert w2_seen_before_w1_done

    asyncio.run(_run(scenario))


def test_renewal_refreshes_content_of_overlapping_days() -> None:
    """Renewing a window whose dates already have days must apply the fresh metas."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient('{"days": [{"day": 1, "meta": "velho"}]}'))
        result = await _register(session, service, user.id)
        assert result["days"][0]["content"] == "velho"

        await _backdate_roadmap(session, result["id"], days=8)

        renew_client = DummyAIClient('{"days": [{"day": 1, "meta": "novo"}, {"day": 2, "meta": "novo2"}]}')
        renewed = await ObjectiveService(session, ai_client=renew_client).renew_roadmap(
            objective_id=result["id"],
            user_id=user.id,
            role="USER",
        )

        assert renewed["days"][0]["content"] == "novo"
        assert renewed["days"][1]["content"] == "novo2"
        total = (
            await session.execute(select(RoadmapDay).where(RoadmapDay.objective_id == result["id"]))
        ).scalars().all()
        assert len(total) == 7  # no duplicates introduced by renewal

    asyncio.run(_run(scenario))


def test_renewal_keeps_completed_status_on_existing_day() -> None:
    """Refreshing content must never wipe an existing day's status."""

    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient('{"days": [{"day": 1, "meta": "velho"}]}'))
        result = await _register(session, service, user.id)
        day_service = RoadmapDayService(session)

        await day_service.set_day_status(
            result["id"], result["days"][0]["id"], user.id, "USER", RoadmapDayStatus.COMPLETED
        )
        await _backdate_roadmap(session, result["id"], days=8)

        renewed = await ObjectiveService(
            session, ai_client=DummyAIClient('{"days": [{"day": 1, "meta": "novo"}]}')
        ).renew_roadmap(result["id"], user_id=user.id, role="USER")

        assert renewed["days"][0]["content"] == "novo"
        assert renewed["days"][0]["status"] == RoadmapDayStatus.COMPLETED
        assert renewed["days"][0]["completed_at"] is not None

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# User isolation
# ---------------------------------------------------------------------------


def test_list_by_user_only_returns_own_objectives() -> None:
    async def scenario(session) -> None:
        owner = await _create_user(session)
        other = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))
        await _register(session, service, owner.id)

        other_service = ObjectiveService(session, ai_client=DummyAIClient("x"))
        assert await other_service.list_by_user(other.id) == []

    asyncio.run(_run(scenario))


def test_non_owner_cannot_list_or_update_days() -> None:
    async def scenario(session) -> None:
        owner = await _create_user(session)
        other = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient("roadmap"))
        result = await _register(session, service, owner.id)
        day_service = RoadmapDayService(session)

        with pytest.raises(HTTPException) as exc:
            await day_service.list_days(result["id"], other.id, "USER")
        assert exc.value.status_code == 403

        with pytest.raises(HTTPException) as exc:
            await day_service.set_day_status(
                result["id"], result["days"][0]["id"], other.id, "USER", RoadmapDayStatus.COMPLETED
            )
        assert exc.value.status_code == 403

        with pytest.raises(HTTPException) as exc:
            await service.renew_roadmap(result["id"], user_id=other.id, role="USER")
        assert exc.value.status_code == 403

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# 404 / cross-objective error handling
# ---------------------------------------------------------------------------


def test_list_or_update_days_for_missing_objective_returns_404() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        day_service = RoadmapDayService(session)

        with pytest.raises(HTTPException) as exc:
            await day_service.list_days(999999, user.id, "USER")
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            await day_service.set_day_status(999999, 1, user.id, "USER", RoadmapDayStatus.COMPLETED)
        assert exc.value.status_code == 404

    asyncio.run(_run(scenario))


def test_day_belonging_to_another_objective_returns_404() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        objective_a = Objective(
            title="A",
            description=None,
            due_date=datetime.now(UTC) + timedelta(days=10),
            user_id=user.id,
        )
        objective_b = Objective(
            title="B",
            description=None,
            due_date=datetime.now(UTC) + timedelta(days=10),
            user_id=user.id,
        )
        session.add_all([objective_a, objective_b])
        await session.commit()

        day_service = RoadmapDayService(session)
        await day_service.create_days(objective_a.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=1))
        days_b = await day_service.create_days(objective_b.id, datetime.now(UTC), datetime.now(UTC) + timedelta(days=1))

        with pytest.raises(HTTPException) as exc:
            # day belongs to objective_b, but objective_a is targeted
            await day_service.set_day_status(objective_a.id, days_b[0].id, user.id, "USER", RoadmapDayStatus.COMPLETED)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Roadmap day not found"

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# Multiple simultaneous roadmaps for the same user
# ---------------------------------------------------------------------------


def test_multiple_roadmaps_for_same_user_are_isolated() -> None:
    async def scenario(session) -> None:
        user = await _create_user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient('{"days": [{"day": 1, "meta": "m1"}]}'))
        first = await _register(session, service, user.id, title="Primeiro")
        second = await _register(session, service, user.id, title="Segundo")

        listed = await service.list_by_user(user.id)
        assert {item["id"] for item in listed} == {first["id"], second["id"]}
        assert len(listed[0]["days"]) == 7 and len(listed[1]["days"]) == 7

        day_service = RoadmapDayService(session)
        days_first = await day_service.list_days(first["id"], user.id, "USER")
        days_second = await day_service.list_days(second["id"], user.id, "USER")
        assert {d.objective_id for d in days_first} == {first["id"]}
        assert {d.objective_id for d in days_second} == {second["id"]}

    asyncio.run(_run(scenario))
