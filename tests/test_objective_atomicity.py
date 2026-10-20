"""Tests for the atomic objective/roadmap writes and the query fixes.

Covers:

1. **Atomic register** — the objective row and its roadmap days are written
   in one transaction, so a failure while inserting days leaves no objective
   behind (and never an objective with a roadmap but no tracking days).
2. **Atomic renew** — same guarantee for the renewal path, plus the 7-day
   window re-checked under a row lock so two concurrent renewals cannot
   both slip through and duplicate the tracking days.
3. **AI output is not lost** — when the write fails, the generated roadmap
   is logged so it can be recovered instead of being discarded with the
   rolled-back transaction.
4. **N+1 removal** — listing objectives and building the general-agent
   context use one grouped day query instead of one query per objective.
5. **No per-row refresh** — ``create_many`` populates primary keys from the
   flush rather than re-reading every inserted row.
6. **Guarded AI day numbers** — a malformed or out-of-window ``day`` value
   from the provider is skipped instead of raising.
"""

import asyncio
import logging
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.commands.general_agent_command import GeneralAgentCommand
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.db.session import Base
from app.models.objective import Objective
from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus
from app.models.user import User
from app.repositories.objective_repository import ObjectiveRepository
from app.repositories.roadmap_day_repository import RoadmapDayRepository
from app.services import general_agent_service as general_agent_module
from app.services import objective_service as objective_module
from app.services.general_agent_service import GeneralAgentService
from app.services.objective_service import ObjectiveService
from app.services.roadmap_day_service import RoadmapDayService
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import make_conversation_cache

_ROADMAP_JSON = (
    '{"days": [' + ", ".join(f'{{"day": {n}, "meta": "meta {n}"}}' for n in range(1, 8)) + "]}"
)


class DummyAIClient:
    def __init__(self, roadmap: str = _ROADMAP_JSON) -> None:
        self.roadmap = roadmap
        self.calls = 0

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        return self.roadmap

    async def create_chat_completion_with_history(
        self, system_prompt: str, messages: list[dict[str, str]]
    ) -> str:
        self.calls += 1
        return self.roadmap


async def _run(scenario) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await scenario(session)
    finally:
        await engine.dispose()


def _command(days: int = 30) -> RegisterObjectiveCommand:
    return RegisterObjectiveCommand(
        title="Aprender FastAPI",
        description="Dominar o framework",
        due_date=datetime.now(UTC) + timedelta(days=days),
    )


async def _user(session) -> User:
    user = User(full_name="Test User", email="test@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _break_day_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the roadmap-day write fail, the way a database hiccup would."""
    original = RoadmapDayService.create_days

    async def exploding(self, *args, **kwargs):
        raise RuntimeError("simulated DB failure while writing roadmap days")

    monkeypatch.setattr(RoadmapDayService, "create_days", exploding)
    # Keep a reference so the intent stays readable even though monkeypatch
    # restores it automatically.
    assert original is not None


def _day_query_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count per-objective vs grouped roadmap-day reads."""
    counts = {"per_objective": 0, "grouped": 0}
    original_single = RoadmapDayRepository.list_by_objective
    original_grouped = RoadmapDayRepository.list_by_objectives

    async def counting_single(self, objective_id):
        counts["per_objective"] += 1
        return await original_single(self, objective_id)

    async def counting_grouped(self, objective_ids):
        counts["grouped"] += 1
        return await original_grouped(self, objective_ids)

    monkeypatch.setattr(RoadmapDayRepository, "list_by_objective", counting_single)
    monkeypatch.setattr(RoadmapDayRepository, "list_by_objectives", counting_grouped)
    return counts


# ---------------------------------------------------------------------------
# 1. Atomic register
# ---------------------------------------------------------------------------


def test_register_writes_objective_and_days_in_one_transaction(monkeypatch) -> None:
    """A day-insert failure must leave no objective row behind."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        _break_day_creation(monkeypatch)

        with pytest.raises(RuntimeError):
            await service.register(_command(), user_id=user_id)

        assert (await session.execute(select(Objective))).scalars().all() == []
        assert (await session.execute(select(RoadmapDay))).scalars().all() == []

    asyncio.run(_run(scenario))


def test_register_rolls_back_but_keeps_the_ai_roadmap_logged(monkeypatch, caplog) -> None:
    """A failed write must not throw away the AI output that was paid for."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        _break_day_creation(monkeypatch)

        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
            await service.register(_command(), user_id=user_id)

        records = [r for r in caplog.records if "NOT persisted" in r.getMessage()]
        assert records, "the generated roadmap must be logged when the write fails"
        record = records[0]
        assert record.unpersisted_roadmap.startswith("## Roadmap")  # type: ignore[attr-defined]
        assert record.unpersisted_day_contents is not None  # type: ignore[attr-defined]
        assert record.unpersisted_action == "register"  # type: ignore[attr-defined]

    asyncio.run(_run(scenario))


def test_register_commits_once_for_objective_and_days(monkeypatch) -> None:
    """Exactly one commit per register: both writes share the transaction."""

    async def scenario(session) -> None:
        user = await _user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient())

        commits = 0
        original_commit = type(session).commit

        def counting_commit(self):
            nonlocal commits
            commits += 1
            return original_commit(self)

        monkeypatch.setattr(type(session), "commit", counting_commit)

        result = await service.register(_command(), user_id=user.id)

        assert commits == 1, f"expected a single commit, saw {commits}"
        assert len(result["days"]) == 7  # type: ignore[arg-type]

    asyncio.run(_run(scenario))


def test_register_survives_a_commit_failure_with_a_usable_session(monkeypatch) -> None:
    """After a failed commit the session must be reusable, not stuck aborted."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        _break_day_creation(monkeypatch)

        with pytest.raises(RuntimeError):
            await service.register(_command(), user_id=user_id)

        assert not session.in_transaction(), "the failed transaction must be rolled back"

        # The very same session must still be usable for the next request.
        monkeypatch.undo()
        follow_up = await service.register(_command(days=40), user_id=user_id)
        assert len(follow_up["days"]) == 7  # type: ignore[arg-type]

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# 2. Atomic renew
# ---------------------------------------------------------------------------


async def _backdate(session, objective_id: int, days: int) -> Objective:
    objective = (
        await session.execute(select(Objective).where(Objective.id == objective_id))
    ).scalar_one()
    objective.roadmap_updated_at = datetime.now(UTC) - timedelta(days=days)
    await session.commit()
    return objective


def test_renew_failure_leaves_the_previous_roadmap_and_days_untouched(monkeypatch) -> None:
    """A failed renewal must not half-write: the old roadmap and days stay."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        before = (
            await session.execute(select(Objective).where(Objective.id == objective_id))
        ).scalar_one()
        original_roadmap = before.roadmap
        original_updated_at = before.roadmap_updated_at
        days_before = await RoadmapDayRepository(session).list_by_objective(objective_id)

        _break_day_creation(monkeypatch)
        with pytest.raises(RuntimeError):
            await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        after = (
            await session.execute(select(Objective).where(Objective.id == objective_id))
        ).scalar_one()
        await session.refresh(after)  # the rollback expired the instance
        assert after.roadmap == original_roadmap, "the roadmap write must be rolled back"
        assert after.roadmap_updated_at == original_updated_at
        days_after = await RoadmapDayRepository(session).list_by_objective(objective_id)
        assert len(days_after) == len(days_before), "no extra days may be left behind"

    asyncio.run(_run(scenario))


def test_renew_failure_reraises_the_original_error(monkeypatch) -> None:
    """The diagnostic must not replace the real failure with a lazy-load error.

    Regression guard: the failure path runs after a rollback, which expires the
    ORM instances.  Reading an attribute from an expired instance triggers a
    lazy reload that raises ``MissingGreenlet``, which would mask the original
    database error and report a misleading 500.
    """

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        _break_day_creation(monkeypatch)
        with pytest.raises(RuntimeError, match="simulated DB failure") as exc:
            await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        assert type(exc.value) is RuntimeError, "the original error must reach the caller"

    asyncio.run(_run(scenario))


def test_renew_failure_logs_the_new_roadmap(monkeypatch, caplog) -> None:
    """The freshly generated renewal roadmap is recoverable when the write fails."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        _break_day_creation(monkeypatch)
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
            await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        records = [r for r in caplog.records if "NOT persisted" in r.getMessage()]
        assert records
        assert records[0].unpersisted_action == "renew"  # type: ignore[attr-defined]
        assert records[0].unpersisted_roadmap.startswith("## Roadmap")  # type: ignore[attr-defined]

    asyncio.run(_run(scenario))


def test_renew_rechecks_the_window_after_the_ai_call(monkeypatch, caplog) -> None:
    """A renewal that raced a winner must get a 409, not a duplicate write.

    The provider call takes seconds, so the 7-day window has to be checked
    again under a row lock immediately before writing.  Simulated here by a
    concurrent request renewing the same objective during the AI call.
    """

    async def scenario(session) -> None:
        from fastapi import HTTPException

        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        class RacingAIClient(DummyAIClient):
            async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
                self.calls += 1
                await _backdate(session, objective_id, days=0)  # the other request won
                return self.roadmap

        service.ai_client = RacingAIClient()  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING), pytest.raises(HTTPException) as exc:
            await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        assert exc.value.status_code == 409
        assert any("NOT persisted" in r.getMessage() for r in caplog.records)
        assert len(await RoadmapDayRepository(session).list_by_objective(objective_id)) == 7

    asyncio.run(_run(scenario))


def test_renew_locks_the_objective_row_for_the_write(monkeypatch) -> None:
    """The write transaction reads the objective with ``FOR UPDATE``."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        original = ObjectiveRepository.get_by_id
        locked_flags: list[bool] = []

        async def spy_get_by_id(self, oid, for_update=False):
            locked_flags.append(for_update)
            return await original(self, oid, for_update=for_update)

        monkeypatch.setattr(ObjectiveRepository, "get_by_id", spy_get_by_id)

        result = await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        assert locked_flags == [False, True], (
            "the renewal must re-read the objective under a row lock before writing"
        )
        assert len(result["days"]) == 7  # type: ignore[arg-type]

    asyncio.run(_run(scenario))


def test_renew_commits_once(monkeypatch) -> None:
    """The roadmap update and the new days share a single commit."""

    async def scenario(session) -> None:
        user_id = (await _user(session)).id
        service = ObjectiveService(session, ai_client=DummyAIClient())
        created = await service.register(_command(days=60), user_id=user_id)
        objective_id = int(created["id"])  # type: ignore[arg-type]
        await _backdate(session, objective_id, days=10)

        commits = 0
        original_commit = type(session).commit

        def counting_commit(self):
            nonlocal commits
            commits += 1
            return original_commit(self)

        monkeypatch.setattr(type(session), "commit", counting_commit)
        await service.renew_roadmap(objective_id, user_id=user_id, role="USER")

        assert commits == 1, f"expected a single commit, saw {commits}"

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# 3. N+1 removal
# ---------------------------------------------------------------------------


async def _seed_objectives(session, user_id: int, count: int, days_per: int = 3) -> None:
    now = datetime.now(UTC)
    objectives = [
        Objective(
            title=f"Objetivo {n}",
            description="desc",
            due_date=now + timedelta(days=30),
            user_id=user_id,
        )
        for n in range(count)
    ]
    session.add_all(objectives)
    await session.commit()
    for objective in objectives:
        await session.flush()
        session.add_all(
            RoadmapDay(
                objective_id=objective.id,
                day_number=d,
                day_date=now + timedelta(days=d - 1),
                content=f"meta {d}",
            )
            for d in range(1, days_per + 1)
        )
    await session.commit()


def test_list_by_user_uses_one_batched_day_query(monkeypatch) -> None:
    """The day read must not grow with the number of objectives."""
    spy = _day_query_spy(monkeypatch)

    async def scenario(session) -> None:
        user = await _user(session)
        await _seed_objectives(session, user.id, count=5)

        listed = await ObjectiveService(session, ai_client=DummyAIClient()).list_by_user(user.id)

        assert len(listed) == 5
        assert all(len(item["days"]) == 3 for item in listed)  # type: ignore[arg-type]

    asyncio.run(_run(scenario))

    assert spy["per_objective"] == 0, "the N+1 read must not be used any more"
    assert spy["grouped"] == 1, "all days are fetched with a single grouped query"


def test_general_agent_context_uses_one_batched_day_query(monkeypatch) -> None:
    """The assistant's context builder must not query per objective."""
    spy = _day_query_spy(monkeypatch)

    async def scenario(session) -> None:
        user = await _user(session)
        await _seed_objectives(session, user.id, count=4)

        service = GeneralAgentService(
            session,
            ai_client=DummyAIClient(),  # type: ignore[arg-type]
            conversation_cache=make_conversation_cache(),
        )
        await service.create_completion(GeneralAgentCommand(user_message="oi"), user.id)

    asyncio.run(_run(scenario))

    assert spy["per_objective"] == 0, "the N+1 read must not be used any more"
    assert spy["grouped"] == 1


def test_list_by_objectives_groups_days_per_objective() -> None:
    """The grouped query returns only the requested objectives' days."""

    async def scenario(session) -> None:
        user = await _user(session)
        now = datetime.now(UTC)
        first = Objective(
            title="A", description=None, due_date=now + timedelta(days=5), user_id=user.id
        )
        second = Objective(
            title="B", description=None, due_date=now + timedelta(days=5), user_id=user.id
        )
        session.add_all([first, second])
        await session.commit()
        session.add_all(
            [
                RoadmapDay(objective_id=first.id, day_number=1, day_date=now, content="a"),
                RoadmapDay(
                    objective_id=first.id,
                    day_number=2,
                    day_date=now + timedelta(days=1),
                    content="b",
                ),
                RoadmapDay(objective_id=second.id, day_number=1, day_date=now, content="c"),
            ]
        )
        await session.commit()

        repo = RoadmapDayRepository(session)
        grouped = await repo.list_by_objectives([first.id])
        assert list(grouped) == [first.id], "only the requested objective is returned"
        assert [d.content for d in grouped[first.id]] == ["a", "b"]
        assert await repo.list_by_objectives([]) == {}, "an empty request must not query"

    asyncio.run(_run(scenario))


def test_list_by_objectives_orders_days_by_calendar_date() -> None:
    """Days come back in the same order as the single-objective query."""

    async def scenario(session) -> None:
        user = await _user(session)
        now = datetime.now(UTC)
        objective = Objective(
            title="A", description=None, due_date=now + timedelta(days=30), user_id=user.id
        )
        session.add(objective)
        await session.commit()
        # Inserted out of order on purpose.
        session.add_all(
            [
                RoadmapDay(objective_id=objective.id, day_number=2, day_date=now + timedelta(days=1), content="b"),
                RoadmapDay(objective_id=objective.id, day_number=1, day_date=now, content="a"),
            ]
        )
        await session.commit()

        repo = RoadmapDayRepository(session)
        grouped = await repo.list_by_objectives([objective.id])
        single = await repo.list_by_objective(objective.id)
        assert [d.content for d in grouped[objective.id]] == [d.content for d in single] == ["a", "b"]

    asyncio.run(_run(scenario))


def test_general_agent_logs_when_the_context_is_truncated() -> None:
    """Overflowing the context budget must be logged, never silent."""

    async def scenario(session) -> None:
        user = await _user(session)
        now = datetime.now(UTC)
        session.add_all(
            [
                Objective(
                    title=f"Objetivo {n}",
                    description=None,
                    due_date=now + timedelta(days=30),
                    user_id=user.id,
                )
                for n in range(general_agent_module.OBJECTIVES_CONTEXT_LIMIT + 3)
            ]
        )
        await session.commit()

        service = GeneralAgentService(
            session,
            ai_client=DummyAIClient(),  # type: ignore[arg-type]
            conversation_cache=make_conversation_cache(),
        )

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture(level=logging.WARNING)
        general_agent_module.logger.addHandler(handler)
        try:
            loaded = await service._load_objective_context(user.id)
        finally:
            general_agent_module.logger.removeHandler(handler)

        assert len(loaded) == general_agent_module.OBJECTIVES_CONTEXT_LIMIT
        assert any("truncated" in r.getMessage() for r in records), (
            "the operator must be able to see that the assistant could not see every objective"
        )

    asyncio.run(_run(scenario))


def test_general_agent_does_not_log_when_nothing_is_truncated() -> None:
    """A user within the limit produces no spurious warning."""

    async def scenario(session) -> None:
        user = await _user(session)
        now = datetime.now(UTC)
        session.add(
            Objective(
                title="Único", description=None, due_date=now + timedelta(days=30), user_id=user.id
            )
        )
        await session.commit()

        service = GeneralAgentService(
            session,
            ai_client=DummyAIClient(),  # type: ignore[arg-type]
            conversation_cache=make_conversation_cache(),
        )

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture(level=logging.WARNING)
        general_agent_module.logger.addHandler(handler)
        try:
            loaded = await service._load_objective_context(user.id)
        finally:
            general_agent_module.logger.removeHandler(handler)

        assert len(loaded) == 1
        assert not any("truncated" in r.getMessage() for r in records)

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# 4. No per-row refresh
# ---------------------------------------------------------------------------


def test_create_many_does_not_reread_each_row() -> None:
    """Inserting 7 days must not re-read any of them."""
    selects: list[str] = []

    async def scenario(session) -> None:
        from sqlalchemy import event

        user = await _user(session)
        now = datetime.now(UTC)
        objective = Objective(
            title="A", description=None, due_date=now + timedelta(days=30), user_id=user.id
        )
        session.add(objective)
        await session.commit()

        engine = session.get_bind()
        target = getattr(engine, "sync_engine", engine)

        def record(_conn, _cursor, statement, *_args) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement.lstrip())

        event.listen(target, "before_cursor_execute", record)
        try:
            days = [
                RoadmapDay(
                    objective_id=objective.id,
                    day_number=n,
                    day_date=now + timedelta(days=n - 1),
                    status=RoadmapDayStatus.PENDING,
                    content=f"meta {n}",
                )
                for n in range(1, 8)
            ]
            await RoadmapDayRepository(session).create_many(days)
        finally:
            event.remove(target, "before_cursor_execute", record)

        assert all(d.id is not None for d in days), "primary keys come from the flush"

    asyncio.run(_run(scenario))

    assert selects == [], f"create_many must not re-read rows, saw {selects}"


# ---------------------------------------------------------------------------
# 5. Guarded AI day numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_day",
    ["dois", None, [1], {"n": 1}, 3.5, True, 0, -1, 99],
)
def test_unusable_ai_day_numbers_are_skipped_not_raised(bad_day) -> None:
    """A malformed ``day`` from the provider must never 500 the request."""
    import json

    async def scenario(session) -> None:
        user = await _user(session)
        payload = json.dumps({"days": [{"day": bad_day, "meta": "ruim"}, {"day": 1, "meta": "boa"}]})
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await service.register(_command(), user_id=user.id)

        assert result["days"][0]["content"] == "boa"  # type: ignore[index]
        assert all(d["content"] != "ruim" for d in result["days"])  # type: ignore[union-attr]
        for line in ("Dia 99", "Dia 0", "Dia -1"):
            assert f"- {line}" not in (result["roadmap"] or "")

    asyncio.run(_run(scenario))


def test_string_day_numbers_from_the_ai_are_accepted() -> None:
    """Providers that quote their numbers must still work."""

    async def scenario(session) -> None:
        user = await _user(session)
        payload = '{"days": [{"day": "1", "meta": "primeiro"}, {"day": "2", "meta": "segundo"}]}'
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await service.register(_command(), user_id=user.id)

        assert result["days"][0]["content"] == "primeiro"  # type: ignore[index]
        assert result["days"][1]["content"] == "segundo"  # type: ignore[index]

    asyncio.run(_run(scenario))


def test_duplicate_ai_day_numbers_keep_one_meta() -> None:
    """A repeated day must appear once, in both the roadmap and the records."""

    async def scenario(session) -> None:
        user = await _user(session)
        payload = '{"days": [{"day": 1, "meta": "a"}, {"day": 1, "meta": "b"}]}'
        service = ObjectiveService(session, ai_client=DummyAIClient(payload))

        result = await service.register(_command(), user_id=user.id)

        assert (result["roadmap"] or "").count("- Dia 1:") == 1
        assert result["days"][0]["content"] == "a"  # type: ignore[index]

    asyncio.run(_run(scenario))


def test_seven_days_are_persisted_end_to_end() -> None:
    """A full seven-day window lands in the database."""
    count = func.count()

    async def scenario(session) -> None:
        user = await _user(session)
        service = ObjectiveService(session, ai_client=DummyAIClient())
        result = await service.register(_command(), user_id=user.id)
        stored = await session.execute(
            select(count).select_from(RoadmapDay).where(RoadmapDay.objective_id == result["id"])
        )
        assert stored.scalar_one() == 7

    asyncio.run(_run(scenario))


def test_objective_service_module_exposes_the_rollback_helper() -> None:
    """Sanity check that the guard used by both write paths is present."""
    assert hasattr(objective_module.ObjectiveService, "_rollback")
    assert hasattr(objective_module.ObjectiveService, "_assert_renewal_allowed")


# ---------------------------------------------------------------------------
# 7. The locked re-read must see the winner's committed value
# ---------------------------------------------------------------------------


async def _run_two_connections(scenario) -> None:
    """Run ``scenario(factory)`` against a *file* SQLite database.

    An in-memory database is served by a single shared connection, so a second
    session would reuse the first one's snapshot and could never reproduce a
    genuine concurrent writer.  A file gives each session its own connection,
    which is what makes the identity-map behaviour observable.
    """
    directory = Path(tempfile.mkdtemp(prefix="novaiax-lock-"))
    engine = create_async_engine(f"sqlite+aiosqlite:///{directory / 'test.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        await scenario(factory)
    finally:
        await engine.dispose()
        shutil.rmtree(directory, ignore_errors=True)


def test_the_locked_re_read_returns_the_committed_row_not_the_cached_one() -> None:
    """``FOR UPDATE`` must refresh the instance already in the identity map.

    The renewal flow loads the objective to check the 7-day window, calls the
    AI (tens of seconds), then re-reads under ``FOR UPDATE``.  SQLAlchemy
    returns the *cached* instance for an already-loaded primary key, so
    without ``populate_existing`` the re-read compared against the pre-AI
    snapshot: two concurrent renewals would both pass the gate and the second
    would overwrite the first's roadmap and duplicate the tracking days.
    """

    async def scenario(factory) -> None:
        from app.repositories.objective_repository import ObjectiveRepository

        async with factory() as writer:
            user = await _user(writer)
            objective = Objective(
                user_id=user.id,
                title="Meta",
                description="d",
                due_date=datetime.now(UTC) + timedelta(days=90),
                roadmap_updated_at=datetime.now(UTC) - timedelta(days=30),
            )
            writer.add(objective)
            await writer.commit()
            objective_id = objective.id

        async with factory() as reader:
            repo = ObjectiveRepository(reader)
            # Mirrors the pre-AI read: the row is now in the identity map.
            cached = await repo.get_by_id(objective_id)
            assert cached is not None
            stale_timestamp = cached.roadmap_updated_at

            # Another request renews first and commits while our AI call runs.
            async with factory() as winner:
                await winner.execute(
                    update(Objective)
                    .where(Objective.id == objective_id)
                    .values(roadmap="roadmap do vencedor", roadmap_updated_at=datetime.now(UTC))
                )
                await winner.commit()

            # The locked re-read must observe the winner's value.
            locked = await repo.get_by_id(objective_id, for_update=True)
            assert locked is not None
            assert locked.roadmap == "roadmap do vencedor"
            assert locked.roadmap_updated_at != stale_timestamp

    asyncio.run(_run_two_connections(scenario))


def test_the_locked_re_read_does_not_overwrite_an_unchanged_row() -> None:
    """The refresh is not a side effect: an untouched row reads back as-is."""

    async def scenario(factory) -> None:
        from app.repositories.objective_repository import ObjectiveRepository

        async with factory() as writer:
            user = await _user(writer)
            objective = Objective(
                user_id=user.id,
                title="Meta",
                description="d",
                due_date=datetime.now(UTC) + timedelta(days=90),
                roadmap="original",
                roadmap_updated_at=datetime.now(UTC) - timedelta(days=30),
            )
            writer.add(objective)
            await writer.commit()
            objective_id = objective.id

        async with factory() as reader:
            repo = ObjectiveRepository(reader)
            first = await repo.get_by_id(objective_id)
            assert first is not None
            locked = await repo.get_by_id(objective_id, for_update=True)
            assert locked is not None
            assert locked.roadmap == "original"
            assert locked.roadmap_updated_at == first.roadmap_updated_at

    asyncio.run(_run_two_connections(scenario))


def test_the_read_transaction_is_closed_before_the_ai_call() -> None:
    """A pooled connection must not be held for the whole provider wait.

    The renewal flow reads the objective and the previous days before calling
    Groq, which left the read transaction — and with it the checked-out
    connection — open for the tens of seconds the provider can take.  Under
    load that drains the pool while requests are merely waiting on the AI.
    """
    observed: list[bool] = []

    async def scenario(session) -> None:
        class _Recording(DummyAIClient):
            def __init__(self) -> None:
                super().__init__("{}")
                self.armed = False

            async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
                if self.armed:
                    observed.append(session.in_transaction())
                return await super().create_chat_completion(system_prompt, user_message)

        user = await _user(session)
        ai = _Recording()
        service = ObjectiveService(session, ai_client=ai)
        created = await service.register(_command(), user_id=user.id)

        await session.execute(
            update(Objective)
            .where(Objective.id == created["id"])
            .values(roadmap_updated_at=datetime.now(UTC) - timedelta(days=10))
        )
        await session.commit()

        ai.armed = True
        assert await service.renew_roadmap(created["id"], user_id=user.id, role="USER") is not None

    asyncio.run(_run(scenario))
    assert observed == [False], "the session must hold no transaction while the AI is called"


def test_register_does_not_open_a_transaction_before_the_ai_call() -> None:
    """The objective and its days are atomic without pinning a connection.

    Writing the objective before calling the provider would keep the write
    transaction — and its pooled connection — open for the whole wait, for no
    benefit: the roadmap is fully generated before anything needs persisting.
    """
    observed: list[bool] = []

    async def scenario(session) -> None:
        class _Recording(DummyAIClient):
            async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
                observed.append(session.in_transaction())
                return await super().create_chat_completion(system_prompt, user_message)

        user = await _user(session)
        user_id = user.id
        # Close the transaction the helper's refresh() opened, so only
        # register's own behaviour is measured.  The rollback expires the
        # instance, hence the id captured above.
        await session.rollback()
        assert not session.in_transaction()

        service = ObjectiveService(session, ai_client=_Recording())
        await service.register(_command(), user_id=user_id)

    asyncio.run(_run(scenario))
    assert observed == [False], "the AI call must happen before the write transaction opens"
