import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest
from app.commands.chat_completion_command import ChatCompletionCommand
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.services.objective_assistant_service import ObjectiveAssistantService

from tests.helpers import make_conversation_cache


class DummyAIClient:
    def __init__(self, response: str) -> None:
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


class StubObjectiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    async def register(self, command, user_id: int, *, plan_tier=None) -> dict[str, object]:
        self.calls.append((command, user_id))
        return {
            "id": 1,
            "title": command.title,
            "description": command.description,
            "due_date": command.due_date,
            "user_id": user_id,
        }


def test_registers_objective_when_ai_returns_complete_json() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(json.dumps({"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "2026-12-31"})),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=7))

    assert result["assistant_message"] == '{"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "2026-12-31"}'
    assert len(objective_service.calls) == 1
    command, user_id = objective_service.calls[0]
    assert command.title == "Estudar"
    assert command.description == "Aprender FastAPI"
    assert user_id == 7


def test_registers_objective_when_ai_returns_json_in_markdown_fence() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(
            'Aqui está sua meta:\n```json\n{"titulo": "Correr", "descricao": "Correr 5km", "prazo": "2026-11-30"}\n```'
        ),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=7))

    assert len(objective_service.calls) == 1
    command, user_id = objective_service.calls[0]
    assert command.title == "Correr"
    assert command.description == "Correr 5km"
    assert user_id == 7


def test_registers_objective_when_ai_returns_json_with_surrounding_text() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(
            'Sua meta foi criada com sucesso: {"titulo": "Ler", "descricao": "Ler um livro por mês", "prazo": "2026-12-31"} Fim.'
        ),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=7))

    assert len(objective_service.calls) == 1
    command, user_id = objective_service.calls[0]
    assert command.title == "Ler"
    assert user_id == 7


def test_ignores_invalid_json_payload_without_registering_objective() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient('{"titulo": "Meta incompleta", "descricao": "Falta prazo"}'),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=3))

    assert result["assistant_message"] == '{"titulo": "Meta incompleta", "descricao": "Falta prazo"}'
    assert objective_service.calls == []


def test_keeps_history_in_cache_while_objective_is_incomplete() -> None:
    objective_service = StubObjectiveService()
    cache = make_conversation_cache()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient("Ainda preciso do prazo, pode me informar?"),
        objective_service=objective_service,
        conversation_cache=cache,
    )

    asyncio.run(
        service.create_completion(
            ObjectiveAssistantCommand(user_message="Quero estudar FastAPI"),
            user_id=7,
        )
    )

    assert await_cache(cache, 7) == [
        {"role": "user", "content": "Quero estudar FastAPI"},
        {"role": "assistant", "content": "Ainda preciso do prazo, pode me informar?"},
    ]


def test_sends_full_history_to_ai_on_next_turn() -> None:
    objective_service = StubObjectiveService()
    ai_client = DummyAIClient("Ainda preciso do prazo, pode me informar?")
    service = ObjectiveAssistantService(
        session=None,
        ai_client=ai_client,
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Quero estudar FastAPI"), user_id=7)
    )
    asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Prazo 2026-12-31"), user_id=7)
    )

    assert ai_client.messages == [
        {"role": "user", "content": "Quero estudar FastAPI"},
        {"role": "assistant", "content": "Ainda preciso do prazo, pode me informar?"},
        {"role": "user", "content": "Prazo 2026-12-31"},
    ]


def test_clears_cache_when_objective_is_created() -> None:
    objective_service = StubObjectiveService()
    cache = make_conversation_cache()
    ai_client = DummyAIClient(
        json.dumps({"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "2026-12-31"})
    )
    service = ObjectiveAssistantService(
        session=None,
        ai_client=ai_client,
        objective_service=objective_service,
        conversation_cache=cache,
    )

    asyncio.run(
        service.create_completion(
            ObjectiveAssistantCommand(user_message="Quero criar uma meta"),
            user_id=7,
        )
    )

    assert objective_service.calls != []
    assert await_cache(cache, 7) == []


def await_cache(cache, user_id: int):
    return asyncio.run(cache.get_messages(ObjectiveAssistantService.AGENT_KEY, user_id))


# ---------------------------------------------------------------------------
# created_objective in the response
# ---------------------------------------------------------------------------


def test_returns_created_objective_when_the_draft_is_registered() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(
            json.dumps({"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "2026-12-31"})
        ),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Quero criar uma meta"), user_id=7)
    )

    assert result["created_objective"] == {
        "id": 1,
        "title": "Estudar",
        "description": "Aprender FastAPI",
        "due_date": datetime(2026, 12, 31, tzinfo=UTC),
        "user_id": 7,
    }


def test_created_objective_is_none_while_collecting_details() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient("Ainda preciso do prazo, pode me informar?"),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Quero estudar FastAPI"), user_id=7)
    )

    assert result["created_objective"] is None
    assert objective_service.calls == []


def test_created_objective_is_none_for_an_incomplete_draft() -> None:
    objective_service = StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient('{"titulo": "Meta incompleta", "descricao": "Falta prazo"}'),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Quero criar uma meta"), user_id=3)
    )

    assert result["created_objective"] is None


# ---------------------------------------------------------------------------
# Unparsable due dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_due_date",
    [
        "31/12/2026",  # day-first locale format
        "12-31-2026",  # US format
        "próximo mês",  # prose
        "2026",  # year only
        "2026-13-45",  # impossible calendar date
        "20261231",  # no separators
    ],
)
def test_unparsable_due_date_asks_the_user_to_confirm_it(
    bad_due_date: str, caplog: pytest.LogCaptureFixture
) -> None:
    objective_service = StubObjectiveService()
    raw = json.dumps({"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": bad_due_date})
    cache = make_conversation_cache()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(raw),
        objective_service=objective_service,
        conversation_cache=cache,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.objective_assistant_service"):
        result = asyncio.run(
            service.create_completion(ObjectiveAssistantCommand(user_message="Quero criar uma meta"), user_id=7)
        )

    # Nothing was persisted, and the user is told what to fix.
    assert objective_service.calls == [], "an unparsable date must not register an objective"
    assert result["created_objective"] is None
    assert result["assistant_message"] == ObjectiveAssistantService.DUE_DATE_CONFIRMATION_MESSAGE
    assert raw not in result["assistant_message"], "the raw JSON must not be echoed back"
    assert "YYYY-MM-DD" in result["assistant_message"], "the reply must state the expected format"

    # The unparsable attempt is logged as a warning rather than swallowed.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the unparsable due date must be logged"
    assert "unparsable due date" in warnings[0].getMessage()

    # The thread is kept so the user can answer with the correct date.
    assert await_cache(cache, 7) == [
        {"role": "user", "content": "Quero criar uma meta"},
        {"role": "assistant", "content": ObjectiveAssistantService.DUE_DATE_CONFIRMATION_MESSAGE},
    ]


def test_unparsable_due_date_can_be_resolved_on_the_next_turn() -> None:
    objective_service = StubObjectiveService()
    cache = make_conversation_cache()
    ai_client = DummyAIClient(
        json.dumps({"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "31/12/2026"})
    )
    service = ObjectiveAssistantService(
        session=None,
        ai_client=ai_client,
        objective_service=objective_service,
        conversation_cache=cache,
    )

    asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="Quero criar uma meta"), user_id=7)
    )
    assert objective_service.calls == []

    # The user replies with a usable date and the objective is finally created.
    ai_client.response = json.dumps(
        {"titulo": "Estudar", "descricao": "Aprender FastAPI", "prazo": "2026-12-31"}
    )
    result = asyncio.run(
        service.create_completion(ObjectiveAssistantCommand(user_message="É 2026-12-31"), user_id=7)
    )

    assert len(objective_service.calls) == 1
    assert result["created_objective"] is not None
    assert result["created_objective"]["title"] == "Estudar"
    assert await_cache(cache, 7) == [], "a created objective still resets the thread"


def test_parse_due_date_accepts_iso_and_rejects_everything_else() -> None:
    parse = ObjectiveAssistantService._parse_due_date
    assert parse("2026-12-31") == datetime(2026, 12, 31, tzinfo=UTC)
    assert parse(" 2026-12-31 ") == datetime(2026, 12, 31, tzinfo=UTC)
    assert parse("31/12/2026") is None
    assert parse("") is None
    assert parse(None) is None
    assert parse(20261231) is None
