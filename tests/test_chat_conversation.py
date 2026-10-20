"""
Tests for the per-user, per-agent conversation history of
:class:`ChatCompletionService` and the ``/ai/chat`` endpoints.

``POST /api/v1/ai/chat`` stores its exchange under
``conversation:<agent_id>:<user_id>`` and replays the previous turns to the
model, so a chat with the same agent keeps its context.  There is no
message-hash response cache any more: replaying an identical message must
still be answered by the model against the accumulated history.

``DELETE /api/v1/ai/chat/conversation?agent_id=<int>`` drops that history and
answers ``404`` when the agent does not exist.
"""

import asyncio
import uuid

import pytest
from app.api.deps import get_ai_router, get_redis_client
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.commands.chat_completion_command import ChatCompletionCommand
from app.commands.clear_chat_conversation_command import ClearChatConversationCommand
from app.core.config import Settings, get_settings
from app.db.session import Base, SessionLocal
from app.main import app
from app.models.system_prompt import SystemPrompt
from app.models.user import User
from app.services.chat_completion_service import ChatCompletionService
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import force_memory_redis

_PASSWORD = "Password123"
_AGENT_PROMPT = "You are a helpful assistant."


class RecordingAIClient:
    """AI double that records every history it is asked to complete."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.system_prompts: list[str] = []

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        self.system_prompts.append(system_prompt)
        self.calls.append(list(messages))
        return f"reply-{len(self.calls)}"


def _memory_redis() -> RedisClient:
    """In-memory Redis (connection to an unreachable port falls back)."""
    return RedisClient(Settings(redis_url="redis://127.0.0.1:1/0"))


async def _with_session(scenario, prompt_text: str = _AGENT_PROMPT) -> None:
    """Run ``scenario(session, agent_id)`` against an in-memory SQLite DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            prompt = SystemPrompt(tipo="general", system_prompt=prompt_text)
            session.add(prompt)
            await session.commit()
            await session.refresh(prompt)
            await scenario(session, int(prompt.id))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def test_history_is_replayed_to_the_model_on_the_next_turn() -> None:
    async def scenario(session, agent_id: int) -> None:
        ai = RecordingAIClient()
        service = ChatCompletionService(session=session, ai_client=ai, cache=_memory_redis())

        first = await service.create_completion(ChatCompletionCommand(agent_id, "my name is Ana"), user_id=1)
        second = await service.create_completion(ChatCompletionCommand(agent_id, "what is my name?"), user_id=1)

        assert first["assistant_message"] == "reply-1"
        assert second["assistant_message"] == "reply-2"
        assert ai.system_prompts == [_AGENT_PROMPT, _AGENT_PROMPT]

        # The second turn must carry the whole thread, not just the new message.
        assert ai.calls[0] == [{"role": "user", "content": "my name is Ana"}]
        assert ai.calls[1] == [
            {"role": "user", "content": "my name is Ana"},
            {"role": "assistant", "content": "reply-1"},
            {"role": "user", "content": "what is my name?"},
        ]

    asyncio.run(_with_session(scenario))


def test_history_is_stored_under_the_documented_key() -> None:
    async def scenario(session, agent_id: int) -> None:
        redis = _memory_redis()
        service = ChatCompletionService(session=session, ai_client=RecordingAIClient(), cache=redis)

        await service.create_completion(ChatCompletionCommand(agent_id, "hello"), user_id=42)

        assert await redis.get(f"conversation:{agent_id}:42") is not None
        assert await redis.keys("ai_response:*") == [], "no message-hash response cache may be written"

    asyncio.run(_with_session(scenario))


def test_two_users_do_not_share_a_thread() -> None:
    async def scenario(session, agent_id: int) -> None:
        ai = RecordingAIClient()
        service = ChatCompletionService(session=session, ai_client=ai, cache=_memory_redis())

        await service.create_completion(ChatCompletionCommand(agent_id, "from user 1"), user_id=1)
        await service.create_completion(ChatCompletionCommand(agent_id, "from user 2"), user_id=2)
        await service.create_completion(ChatCompletionCommand(agent_id, "again from user 1"), user_id=1)

        assert ai.calls[1] == [{"role": "user", "content": "from user 2"}], "user 2 must start empty"
        assert [m["content"] for m in ai.calls[2]] == ["from user 1", "reply-1", "again from user 1"]

    asyncio.run(_with_session(scenario))


def test_two_agents_of_the_same_user_do_not_share_a_thread() -> None:
    async def scenario(session, agent_id: int) -> None:
        other = SystemPrompt(tipo="support", system_prompt="Be terse.")
        session.add(other)
        await session.commit()
        await session.refresh(other)

        ai = RecordingAIClient()
        service = ChatCompletionService(session=session, ai_client=ai, cache=_memory_redis())

        await service.create_completion(ChatCompletionCommand(agent_id, "general question"), user_id=1)
        await service.create_completion(ChatCompletionCommand(int(other.id), "support question"), user_id=1)

        assert [m["content"] for m in ai.calls[1]] == ["support question"]
        assert ai.system_prompts == [_AGENT_PROMPT, "Be terse."]

    asyncio.run(_with_session(scenario))


def test_identical_message_is_not_served_from_a_response_cache() -> None:
    async def scenario(session, agent_id: int) -> None:
        ai = RecordingAIClient()
        service = ChatCompletionService(session=session, ai_client=ai, cache=_memory_redis())

        cmd = ChatCompletionCommand(agent_id, "same message")
        first = await service.create_completion(cmd, user_id=1)
        second = await service.create_completion(cmd, user_id=1)

        # The model runs again, now seeing the first exchange.
        assert len(ai.calls) == 2
        assert first["assistant_message"] == "reply-1"
        assert second["assistant_message"] == "reply-2"
        assert [m["content"] for m in ai.calls[1]] == ["same message", "reply-1", "same message"]

    asyncio.run(_with_session(scenario))


def test_unknown_agent_raises_404_without_calling_the_ai() -> None:
    async def scenario(session, agent_id: int) -> None:
        from fastapi import HTTPException

        ai = RecordingAIClient()
        service = ChatCompletionService(session=session, ai_client=ai, cache=_memory_redis())

        with pytest.raises(HTTPException) as excinfo:
            await service.create_completion(ChatCompletionCommand(999_999, "hi"), user_id=1)

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Agente não encontrado."
        assert ai.calls == []

    asyncio.run(_with_session(scenario))


def test_clear_conversation_drops_only_that_agent_and_user() -> None:
    async def scenario(session, agent_id: int) -> None:
        other = SystemPrompt(tipo="support", system_prompt="Be terse.")
        session.add(other)
        await session.commit()
        await session.refresh(other)

        redis = _memory_redis()
        service = ChatCompletionService(session=session, ai_client=RecordingAIClient(), cache=redis)
        await service.create_completion(ChatCompletionCommand(agent_id, "a"), user_id=1)
        await service.create_completion(ChatCompletionCommand(int(other.id), "b"), user_id=1)
        await service.create_completion(ChatCompletionCommand(agent_id, "c"), user_id=2)

        await service.clear_conversation(ClearChatConversationCommand(agent_id), user_id=1)

        assert await redis.get(f"conversation:{agent_id}:1") is None
        assert await redis.get(f"conversation:{other.id}:1") is not None, "other agent must survive"
        assert await redis.get(f"conversation:{agent_id}:2") is not None, "other user must survive"

    asyncio.run(_with_session(scenario))


def test_clear_conversation_raises_404_for_unknown_agent() -> None:
    async def scenario(session, agent_id: int) -> None:
        from fastapi import HTTPException

        redis = _memory_redis()
        service = ChatCompletionService(session=session, ai_client=RecordingAIClient(), cache=redis)
        await service.create_completion(ChatCompletionCommand(agent_id, "a"), user_id=1)

        with pytest.raises(HTTPException) as excinfo:
            await service.clear_conversation(ClearChatConversationCommand(999_999), user_id=1)

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Agente não encontrado."
        assert await redis.get(f"conversation:{agent_id}:1") is not None, "a 404 must not wipe anything"

    asyncio.run(_with_session(scenario))


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate cached state and lift the AI rate limit for these tests."""
    force_memory_redis(monkeypatch)
    monkeypatch.setattr(get_settings(), "rate_limit_ai", 10_000)


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    """Keep the test database deterministic (users and agents made here)."""
    yield

    async def purge() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email.like("chat-conv-%@example.com")))
            await session.execute(delete(SystemPrompt).where(SystemPrompt.tipo.like("chat-conv-%")))
            await session.commit()

    asyncio.run(purge())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient) -> dict:
    email = f"chat-conv-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Chat User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_prompt() -> int:
    async def run() -> int:
        async with SessionLocal() as session:
            prompt = SystemPrompt(tipo=f"chat-conv-{uuid.uuid4().hex[:8]}", system_prompt=_AGENT_PROMPT)
            session.add(prompt)
            await session.commit()
            await session.refresh(prompt)
            return int(prompt.id)

    return asyncio.run(run())


def _shared_conversation_cache() -> ConversationCache:
    """The cache instance the running app uses (same injected client)."""
    return ConversationCache(asyncio.run(get_redis_client()))


def _stored(agent_id: int, user_id: int) -> list[dict[str, str]]:
    return asyncio.run(_shared_conversation_cache().get_messages(str(agent_id), user_id))


def _fake_ai(monkeypatch: pytest.MonkeyPatch) -> RecordingAIClient:
    client = RecordingAIClient()
    # Feed the chat route the fake through DI.  ``RecordingAIClient`` is not an
    # ``AIRouter``, so ``run_ai_request`` calls its duck-typed
    # ``create_chat_completion_with_history`` directly.
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides})
    app.dependency_overrides[get_ai_router] = lambda: client
    return client


def test_chat_endpoint_keeps_history_between_requests(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    ai = _fake_ai(monkeypatch)
    agent_id = _create_prompt()
    tokens = _register(client)
    user_id = client.get("/api/v1/auth/me", headers=_auth(tokens["access_token"])).json()["id"]

    first = client.post(
        "/api/v1/ai/chat",
        json={"agent_id": agent_id, "user_message": "my name is Ana"},
        headers=_auth(tokens["access_token"]),
    )
    second = client.post(
        "/api/v1/ai/chat",
        json={"agent_id": agent_id, "user_message": "what is my name?"},
        headers=_auth(tokens["access_token"]),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    body = second.json()
    assert body.pop("message_id")
    assert body == {"assistant_message": "reply-2", "why": [], "sources": []}
    assert [m["content"] for m in ai.calls[1]] == ["my name is Ana", "reply-1", "what is my name?"]

    # The thread is stored under conversation:<agent_id>:<user_id>.
    assert [m["content"] for m in _stored(agent_id, user_id)] == [
        "my name is Ana",
        "reply-1",
        "what is my name?",
        "reply-2",
    ]


def test_delete_conversation_endpoint_clears_history(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _fake_ai(monkeypatch)
    agent_id = _create_prompt()
    tokens = _register(client)
    user_id = client.get("/api/v1/auth/me", headers=_auth(tokens["access_token"])).json()["id"]

    client.post(
        "/api/v1/ai/chat",
        json={"agent_id": agent_id, "user_message": "remember me"},
        headers=_auth(tokens["access_token"]),
    )
    assert _stored(agent_id, user_id), "precondition: a history exists"

    resp = client.delete(
        f"/api/v1/ai/chat/conversation?agent_id={agent_id}",
        headers=_auth(tokens["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agent_id": agent_id, "cleared": True}
    assert _stored(agent_id, user_id) == []


def test_delete_conversation_is_idempotent(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    _fake_ai(monkeypatch)
    agent_id = _create_prompt()
    tokens = _register(client)

    for _ in range(2):
        resp = client.delete(
            f"/api/v1/ai/chat/conversation?agent_id={agent_id}",
            headers=_auth(tokens["access_token"]),
        )
        assert resp.status_code == 200, resp.text


def test_delete_conversation_404s_for_unknown_agent(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _fake_ai(monkeypatch)
    tokens = _register(client)

    resp = client.delete("/api/v1/ai/chat/conversation?agent_id=999999", headers=_auth(tokens["access_token"]))

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Agente não encontrado."


def test_delete_conversation_requires_the_agent_id(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.delete("/api/v1/ai/chat/conversation", headers=_auth(tokens["access_token"]))
    assert resp.status_code == 422


def test_delete_conversation_requires_authentication(client: TestClient) -> None:
    agent_id = _create_prompt()
    assert client.delete(f"/api/v1/ai/chat/conversation?agent_id={agent_id}").status_code == 401


def test_delete_conversation_rejects_api_key_identity(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    agent_id = _create_prompt()
    monkeypatch.setattr(get_settings(), "master_api_key", "test-master-key")
    resp = client.delete(
        f"/api/v1/ai/chat/conversation?agent_id={agent_id}",
        headers={"X-API-Key": "test-master-key"},
    )
    assert resp.status_code == 403


def test_delete_conversation_only_affects_the_caller(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _fake_ai(monkeypatch)
    agent_id = _create_prompt()
    tokens_a = _register(client)
    tokens_b = _register(client)
    id_a = client.get("/api/v1/auth/me", headers=_auth(tokens_a["access_token"])).json()["id"]
    id_b = client.get("/api/v1/auth/me", headers=_auth(tokens_b["access_token"])).json()["id"]

    for token in (tokens_a["access_token"], tokens_b["access_token"]):
        client.post(
            "/api/v1/ai/chat",
            json={"agent_id": agent_id, "user_message": "hello"},
            headers=_auth(token),
        )

    client.delete(
        f"/api/v1/ai/chat/conversation?agent_id={agent_id}",
        headers=_auth(tokens_a["access_token"]),
    )

    assert _stored(agent_id, id_a) == []
    assert _stored(agent_id, id_b), "another user's thread must survive"
