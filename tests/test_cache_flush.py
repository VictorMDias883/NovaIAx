"""
Tests for the per-user cache-flush endpoint (``POST /api/v1/cache/flush``).

Covers:

1. **Service layer** — :func:`flush_user_cache` deletes every conversation
   history and reverse-proxy cache entry owned by one user while leaving other
   users and global state (``ai_response`` prompt cache, rate-limit counters,
   token denylist) untouched, and reports per-category deletion counts.
2. **Endpoint security** — unauthenticated requests get a ``401`` and
   API-key (SERVICE) identities get a clean ``403`` instead of crashing on
   ``int("api-key")``.
3. **End-to-end** — a real user's cache is cleared through the HTTP layer and
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

_PASSWORD = "Password123"


def _shared_redis() -> RedisClient:
    """In-memory Redis (connection to an unreachable port falls back)."""
    return RedisClient(Settings(redis_url="redis://127.0.0.1:1/0"))


def _force_memory_redis(monkeypatch: pytest.MonkeyPatch) -> RedisClient:
    """Inject one shared in-memory :class:`RedisClient` into the DI container."""
    shared = _shared_redis()
    monkeypatch.setattr("app.api.deps.RedisClient", lambda *a, **k: shared)
    return shared


def _unique_email(prefix: str) -> str:
    return f"{prefix}-flush-{uuid.uuid4().hex[:8]}@example.com"


def _register(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Flush User", "email": email, "password": _PASSWORD},
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

        # Shared prompt-digest cache must survive a per-user flush.
        await redis.set("ai_response:general_agent:deadbeef", "shared")

        from app.services.cache_flush_service import flush_user_cache

        cleared = await flush_user_cache(redis, 1)

        assert cleared == {"conversations_cleared": 2, "proxy_entries_cleared": 2}
        assert "ai_response:general_agent:deadbeef" in await redis.keys("*")
        assert not await redis.keys("conversation:*:1"), "user 1 histories must be gone"
        assert await redis.keys("conversation:*:2"), "user 2 histories must survive"
        assert await redis.smembers("proxy_cache:index:1") == set(), "user 1 index must be gone"
        assert not await redis.keys("proxy:key:"), "user 1 proxy entries must be gone"
        assert "proxy:key:3" in await redis.keys("*"), "user 2 proxy entries must survive"
        assert await redis.smembers("proxy_cache:index:2") == {"proxy:key:3"}

    asyncio.run(scenario())


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


def test_flush_clears_the_callers_cache_via_http(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
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

    resp = client.post("/api/v1/cache/flush", headers=_auth(user_a["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["user_id"] == id_a
    assert body["cleared"]["conversations_cleared"] == 1

    assert not asyncio.run(shared.keys(f"conversation:*:{id_a}"))
    assert asyncio.run(shared.keys(f"conversation:*:{id_b}")), "user B's cache must survive"
