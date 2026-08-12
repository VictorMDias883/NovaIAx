import asyncio
import json

from app.commands.chat_completion_command import ChatCompletionCommand
from app.services.objective_assistant_service import ObjectiveAssistantService


class DummyAIClient:
    def __init__(self, response: str) -> None:
        self.response = response

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.response


class StubObjectiveService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    async def register(self, command, user_id: int) -> dict[str, object]:
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
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=7))

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
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=7))

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
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="Quero criar uma meta"), user_id=3))

    assert result["assistant_message"] == '{"titulo": "Meta incompleta", "descricao": "Falta prazo"}'
    assert objective_service.calls == []
