import asyncio
import json

from app.commands.chat_completion_command import ChatCompletionCommand
from app.services.objective_assistant_service import ObjectiveAssistantService
from tests.helpers import make_conversation_cache


class DummyAIClient:
    def __init__(self, response: str) -> None:
        self.response = response

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        return self.response

    async def create_chat_completion_with_history(self, system_prompt: str, messages: list[dict[str, str]]) -> str:
        return self.response


class StubObjectiveService:
    def __init__(self) -> None:
        self.calls = []

    async def register(self, command, user_id: int) -> dict:
        self.calls.append((command, user_id))
        return {"id": 1, "title": command.title, "description": command.description, "due_date": command.due_date, "user_id": user_id}


def test_registers_objective_when_ai_returns_english_json() -> None:
    # AI returns English keys instead of Portuguese; service should accept them.
    objective_service = StubObjectiveService()
    payload = json.dumps({"title": "Learn English", "description": "Daily practice", "due_date": "2026-12-31"})
    service = ObjectiveAssistantService(
        session=None,
        ai_client=DummyAIClient(payload),
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    result = asyncio.run(service.create_completion(ChatCompletionCommand(agent_id=1, user_message="I want to create an objective"), user_id=42))

    assert result["assistant_message"] == payload
    assert len(objective_service.calls) == 1
    command, uid = objective_service.calls[0]
    assert command.title == "Learn English"
    assert command.description == "Daily practice"
    assert uid == 42
