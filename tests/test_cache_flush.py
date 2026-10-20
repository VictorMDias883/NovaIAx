"""
Tests for the per-user cache-flush endpoint (``POST /api/v1/cache/flush``).

Covers:

1. **Service layer** — :func:`flush_user_cache` deletes a user's AI chat
   responses and reverse-proxy cache entries while leaving other users and
   global state (rate-limit counters, token denylist) untouched, and reports
   per-category deletion counts.
2. **Conversation history is preserved by default** — a routine flush must not
   wipe the assistant's memory of the current thread; deletion happens only
   when ``include_conversations=true`` is passed.
3. **Endpoint security** — unauthenticated requests get a ``401`` and
   API-key (SERVICE) identities get a clean ``403`` instead of crashing on
   ``int("api-key")``.
4. **End-to-end** — a real user's cache is cleared through the HTTP layer and
   another user's data survives.
"""

import asyncio
import uuid

import pytest
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.core.config import Settings
from app.db.session import SessionLocal
from app.main import app
from app.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tests.helpers import force_memory_redis

_PASSWORD = "Password123"


def _shared_redis() -> RedisClient:
    """In-memory Redis (connection to an unreachable port falls back)."""
    return RedisClient(Settings(redis_url="redis://127.0.0.1:1/0"))


def _force_memory_redis(monkeypatch: pytest.MonkeyPatch) -> RedisClient:
    """Inject one shared in-memory :class:`RedisClient` into the DI container."""
    return force_memory_redis(monkeypatch)


def _unique_email(prefix: str) -> str:
    return f"{prefix}-flush-{uuid.uuid4().hex[:8]}@example.com"


def _register(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Flush User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    """Keep ``test.db`` deterministic by removing users created here."""
    async def purge() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email.like("%-flush-@example.com")))
            await session.commit()

    yield
    asyncio.run(purge())


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def test_flush_user_cache_clears_only_that_user() -> None:
    async def scenario() -> None:
        redis = _shared_redis()
        cache = ConversationCache(redis)

        # User 1 owns histories with two assistants; user 2 with one.
        await cache.save("general_agent", 1, [{"role": "user", "content": "oi"}])
        await cache.save("objective_assistant", 1, [{"role": "user", "content": "oi"}])
        await cache.save("general_agent", 2, [{"role": "user", "content": "oi"}])

        # Proxy response-cache entries (indexed per user).
        await redis.sadd("proxy_cache:index:1", "proxy:key:1", "proxy:key:2")
        await redis.set("proxy:key:1", "a")
        await redis.set("proxy:key:2", "b")
        await redis.sadd("proxy_cache:index:2", "proxy:key:3")
        await redis.set("proxy:key:3", "c")

        # User-scoped AI chat-response cache entries: user 1 has two responses,
        # user 2 has one.
        await redis.set("ai_response:gpt:1:cafe", "u1-answer")
        await redis.set("ai_response:gpt:1:beef", "u1-answer-2")
        await redis.set("ai_response:gpt:2:cafe", "u2-answer")

        from app.services.cache_flush_service import flush_user_cache

        cleared = await flush_user_cache(redis, 1, include_conversations=True)

        assert cleared == {"conversations_cleared": 2, "ai_responses_cleared": 2, "proxy_entries_cleared": 2}
        assert not await redis.keys("ai_response:*:1:*"), "user 1 AI responses must be gone"
        assert await redis.keys("ai_response:*:2:*"), "user 2 AI responses must survive"
        assert not await redis.keys("conversation:*:1"), "user 1 histories must be gone"
        assert await redis.keys("conversation:*:2"), "user 2 histories must survive"
        assert await redis.smembers("proxy_cache:index:1") == set(), "user 1 index must be gone"
        assert not await redis.keys("proxy:key:"), "user 1 proxy entries must be gone"
        assert "proxy:key:3" in await redis.keys("*"), "user 2 proxy entries must survive"
        assert await redis.smembers("proxy_cache:index:2") == {"proxy:key:3"}

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Conversation history is opt-in
# ---------------------------------------------------------------------------


def test_flush_user_cache_keeps_conversations_by_default() -> None:
    async def scenario() -> None:
        from app.services.cache_flush_service import flush_user_cache

        redis = _shared_redis()
        cache = ConversationCache(redis)
        await cache.save("general_agent", 1, [{"role": "user", "content": "oi"}])
        await cache.save("objective_assistant", 1, [{"role": "user", "content": "oi"}])
        await redis.set("ai_response:gpt:1:cafe", "u1-answer")
        await redis.sadd("proxy_cache:index:1", "proxy:key:1")

        # The default must not touch the conversation histories.
        cleared = await flush_user_cache(redis, 1)

        assert cleared["conversations_cleared"] == 0
        assert cleared["ai_responses_cleared"] == 1
        assert cleared["proxy_entries_cleared"] == 1
        assert len(await redis.keys("conversation:*:1")) == 2, "histories must survive"
        assert await cache.get_messages("general_agent", 1) == [{"role": "user", "content": "oi"}]
        assert not await redis.keys("ai_response:*:1:*"), "the derived AI responses are still flushed"
        assert not await redis.keys("proxy:key:")

    asyncio.run(scenario())


def test_flush_user_cache_removes_conversations_when_opted_in() -> None:
    async def scenario() -> None:
        from app.services.cache_flush_service import flush_user_cache

        redis = _shared_redis()
        cache = ConversationCache(redis)
        await cache.save("general_agent", 1, [{"role": "user", "content": "oi"}])
        await cache.save("7", 1, [{"role": "user", "content": "oi"}])
        await cache.save("general_agent", 2, [{"role": "user", "content": "oi"}])

        cleared = await flush_user_cache(redis, 1, include_conversations=True)

        assert cleared["conversations_cleared"] == 2
        assert not await redis.keys("conversation:*:1")
        assert await redis.keys("conversation:*:2"), "user 2 histories must survive"

    asyncio.run(scenario())


def test_flush_user_cache_signature_defaults_to_preserving_conversations() -> None:
    """Guard the default at the signature level, so a caller cannot opt in by
    accident when adding a new argument."""
    import inspect

    from app.services.cache_flush_service import flush_user_cache

    parameter = inspect.signature(flush_user_cache).parameters["include_conversations"]
    assert parameter.default is False


# ---------------------------------------------------------------------------
# Endpoint security
# ---------------------------------------------------------------------------


def test_flush_requires_authentication(client: TestClient) -> None:
    resp = client.post("/api/v1/cache/flush")
    assert resp.status_code == 401


def test_flush_rejects_api_key_identity(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "master_api_key", "test-master-key")
    resp = client.post("/api/v1/cache/flush", headers={"X-API-Key": "test-master-key"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_flush_keeps_the_callers_conversation_via_http(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """The default flush must not delete the caller's conversation history."""
    shared = _force_memory_redis(monkeypatch)

    user_a = _register(client, _unique_email("a"))
    user_b = _register(client, _unique_email("b"))

    me_a = client.get("/api/v1/auth/me", headers=_auth(user_a["access_token"]))
    me_b = client.get("/api/v1/auth/me", headers=_auth(user_b["access_token"]))
    assert me_a.status_code == 200 and me_b.status_code == 200
    id_a, id_b = me_a.json()["id"], me_b.json()["id"]

    cache = ConversationCache(shared)
    asyncio.run(cache.save("general_agent", id_a, [{"role": "user", "content": "oi"}]))
    asyncio.run(cache.save("objective_assistant", id_b, [{"role": "user", "content": "oi"}]))
    asyncio.run(shared.set("ai_response:gpt:1:cafe", "stale"))

    resp = client.post("/api/v1/cache/flush", headers=_auth(user_a["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["user_id"] == id_a
    assert body["cleared"]["conversations_cleared"] == 0

    assert asyncio.run(cache.get_messages("general_agent", id_a)) == [
        {"role": "user", "content": "oi"}
    ], "the caller's thread must survive a default flush"
    assert asyncio.run(cache.get_messages("objective_assistant", id_b)), "user B must survive too"


def test_flush_removes_conversations_when_opted_in_via_http(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    shared = _force_memory_redis(monkeypatch)

    user_a = _register(client, _unique_email("a"))
    user_b = _register(client, _unique_email("b"))
    id_a = client.get("/api/v1/auth/me", headers=_auth(user_a["access_token"])).json()["id"]
    id_b = client.get("/api/v1/auth/me", headers=_auth(user_b["access_token"])).json()["id"]

    cache = ConversationCache(shared)
    asyncio.run(cache.save("general_agent", id_a, [{"role": "user", "content": "oi"}]))
    asyncio.run(cache.save("general_agent", id_b, [{"role": "user", "content": "oi"}]))

    resp = client.post(
        "/api/v1/cache/flush?include_conversations=true",
        headers=_auth(user_a["access_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleared"]["conversations_cleared"] == 1

    assert not asyncio.run(shared.keys(f"conversation:*:{id_a}"))
    assert asyncio.run(shared.keys(f"conversation:*:{id_b}")), "user B's cache must survive"


def test_flush_rejects_a_non_boolean_include_conversations(client: TestClient) -> None:
    user_a = _register(client, _unique_email("a"))
    resp = client.post(
        "/api/v1/cache/flush?include_conversations=maybe",
        headers=_auth(user_a["access_token"]),
    )
    assert resp.status_code == 422
