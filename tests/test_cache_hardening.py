"""Tests for the cache-layer hardening.

Covers:

1. **In-memory TTLs are real** — a key set with ``ex`` disappears once its
   deadline passes, and re-setting it without ``ex`` makes it permanent.
   The store previously documented that TTLs were *not* enforced, which meant
   conversation history and cached responses survived forever whenever Redis
   was unreachable.
2. **``SCAN`` instead of ``KEYS``** — key discovery uses the cursor-based
   command, which does not block the server.
3. **Batched deletion** — keys are removed through one pipelined
   ``UNLINK``/``DEL`` batch rather than one round trip per key.
4. **Flush behaviour is unchanged** — the same keys are removed and the same
   counters are reported.
"""

import asyncio

import pytest
from app.cache.redis_client import InMemoryStore, RedisClient
from app.core.config import Settings


def _client() -> RedisClient:
    """A client guaranteed to use the in-memory store."""
    return RedisClient(Settings(redis_url="redis://127.0.0.1:1/0"))


# ---------------------------------------------------------------------------
# 1. In-memory TTLs
# ---------------------------------------------------------------------------


def test_in_memory_ttl_expires_the_key() -> None:
    """A key with a TTL must stop being readable once it elapses."""

    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("k", "v", ex=1)
        assert await store.get("k") == "v"
        # Fast-forward: sleep just past the deadline rather than a full second.
        await asyncio.sleep(1.05)
        assert await store.get("k") is None, "an expired key must read as absent"

    asyncio.run(scenario())


def test_in_memory_ttl_keeps_the_key_before_it_elapses() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("k", "v", ex=5)
        await asyncio.sleep(0.05)
        assert await store.get("k") == "v"

    asyncio.run(scenario())


def test_in_memory_set_without_ttl_is_permanent() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("k", "v")
        await asyncio.sleep(0.05)
        assert await store.get("k") == "v"

    asyncio.run(scenario())


def test_in_memory_set_without_ex_clears_a_previous_deadline() -> None:
    """Overwriting a key with a plain ``set`` must not leave the old TTL."""
    store = InMemoryStore()

    async def scenario() -> None:
        await store.set("k", "v", ex=1)
        assert "k" in store._expiry
        await store.set("k", "v2")
        assert "k" not in store._expiry, "a plain SET makes the key permanent again"
        assert await store.get("k") == "v2"

    asyncio.run(scenario())


def test_in_memory_expire_sets_a_deadline() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("k", "v")
        await store.expire("k", 1)
        assert await store.get("k") == "v"
        await asyncio.sleep(1.05)
        assert await store.get("k") is None

    asyncio.run(scenario())


def test_in_memory_expire_on_a_missing_key_is_a_noop() -> None:
    """Mirrors Redis, where ``EXPIRE`` on a missing key is a no-op."""

    async def scenario() -> None:
        store = InMemoryStore()
        await store.expire("absent", 10)
        assert await store.get("absent") is None

    asyncio.run(scenario())


def test_in_memory_keys_hides_expired_entries() -> None:
    """A pattern scan must not return keys that have already expired."""

    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("conversation:general:1", "a", ex=1)
        await store.set("conversation:general:2", "b", ex=600)
        await asyncio.sleep(1.05)
        assert await store.keys("conversation:general:*") == ["conversation:general:2"]

    asyncio.run(scenario())


def test_in_memory_delete_clears_the_deadline() -> None:
    async def scenario() -> None:
        store = InMemoryStore()
        await store.set("k", "v", ex=1)
        await store.delete("k")
        assert await store.get("k") is None
        assert store._expiry == {}

    asyncio.run(scenario())


def test_reset_memory_store_clears_deadlines() -> None:
    """Test isolation must not leave stale deadlines behind."""
    client = _client()

    async def scenario() -> None:
        store = client._memory_store
        await store.set("k", "v", ex=600)
        assert store._expiry
        client.reset_memory_store()
        assert store._expiry == {}

    asyncio.run(scenario())


def test_ttl_survives_a_redis_client_wrapper() -> None:
    """The TTL reaches the store through the public ``RedisClient.set``."""

    async def scenario() -> None:
        client = _client()
        await client.get_client()  # force the fallback decision
        await client.set("k", "v", ex=1)
        assert await client.get("k") == "v"
        await asyncio.sleep(1.05)
        assert await client.get("k") is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. SCAN instead of KEYS
# ---------------------------------------------------------------------------


def test_scan_keys_is_used_instead_of_keys() -> None:
    """Discovery must not call Redis ``KEYS``, which blocks the whole server."""
    client = _client()
    scanned: list[str] = []

    async def scenario() -> None:
        store = client._memory_store
        original_keys = store.keys
        original_client_keys = client.keys

        async def record(pattern):
            scanned.append(pattern)
            return []

        async def forbidden(pattern):
            raise AssertionError("the blocking KEYS command must not be used")

        store.keys = record  # type: ignore[method-assign]
        client.keys = forbidden  # type: ignore[method-assign]
        try:
            await client.scan_keys("ai_response:*:1:*")
        finally:
            store.keys = original_keys  # type: ignore[method-assign]
            client.keys = original_client_keys  # type: ignore[method-assign]

    asyncio.run(scenario())
    assert scanned == ["ai_response:*:1:*"]


def test_scan_keys_matches_a_glob_pattern() -> None:
    client = _client()

    async def scenario() -> None:
        await client.get_client()
        await client.set("ai_response:general:7:abc", "x")
        await client.set("ai_response:general:8:abc", "x")
        await client.set("ai_response:general:7:def", "y")
        found = await client.scan_keys("ai_response:*:7:*")
        assert sorted(found) == ["ai_response:general:7:abc", "ai_response:general:7:def"]

    asyncio.run(scenario())


def test_scan_keys_returns_nothing_for_no_match() -> None:
    client = _client()

    async def scenario() -> None:
        await client.get_client()
        await client.set("other", "x")
        assert await client.scan_keys("ai_response:*") == []

    asyncio.run(scenario())


class _FakePipe:
    """Records the commands queued on one pipeline."""

    def __init__(self, name: str = "unlink") -> None:
        self.name = name
        self.commands: list[tuple[str, str]] = []

    def unlink(self, key: str) -> None:
        self.commands.append(("unlink", key))

    def delete(self, key: str) -> None:
        self.commands.append(("delete", key))

    async def execute(self) -> list[int]:
        return [1] * len(self.commands)


class _FakeRedis:
    """A live-looking client exposing only ``scan_iter`` and ``pipeline``."""

    def __init__(self) -> None:
        self.scan_calls: list[dict] = []
        self.pipes: list[_FakePipe] = []

    def scan_iter(self, match=None, count=None):
        self.scan_calls.append({"match": match, "count": count})

        async def _gen():
            for key in ("a", "b"):
                yield key

        return _gen()

    def pipeline(self, transaction=False) -> _FakePipe:
        pipe = _FakePipe()
        self.pipes.append(pipe)
        return pipe

    def unlink(self, *keys: str) -> None:
        """Presence of this attribute is how Redis 4+ support is detected."""


class _FakeRedisNoUnlink:
    """Same, but without ``UNLINK`` (Redis < 4)."""

    def __init__(self) -> None:
        self.pipes: list[_FakePipe] = []

    def pipeline(self, transaction=False) -> _FakePipe:
        pipe = _FakePipe()
        self.pipes.append(pipe)
        return pipe


def test_scan_keys_uses_the_cursor_on_a_real_client() -> None:
    """A live client is iterated with ``scan_iter``, not ``keys``."""

    async def scenario() -> None:
        client = _client()
        fake = _FakeRedis()
        client._client = fake

        assert sorted(await client.scan_keys("pattern:*")) == ["a", "b"]
        assert fake.scan_calls == [{"match": "pattern:*", "count": 500}]

        assert await client.delete_many(["a", "b"]) == 2
        assert [command for pipe in fake.pipes for command in pipe.commands] == [
            ("unlink", "a"),
            ("unlink", "b"),
        ]

    asyncio.run(scenario())


def test_scan_keys_falls_back_to_memory_on_failure() -> None:
    """A broken connection degrades to the in-memory store, not an error."""

    class _BrokenRedis:
        def scan_iter(self, match=None, count=None):
            raise RuntimeError("connection lost")

    async def scenario() -> None:
        client = _client()
        await client.get_client()
        await client._memory_store.set("ai_response:a:1:x", "v")
        client._client = _BrokenRedis()
        assert await client.scan_keys("ai_response:*") == ["ai_response:a:1:x"]
        assert client._using_memory(), "the client must pin itself to the fallback"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. Batched deletion
# ---------------------------------------------------------------------------


def test_delete_many_deletes_every_key_in_one_pipeline() -> None:
    client = _client()

    async def scenario() -> None:
        await client.get_client()
        for n in range(5):
            await client.set(f"k{n}", "v")
        assert await client.delete_many([f"k{n}" for n in range(5)]) == 5
        for n in range(5):
            assert await client.get(f"k{n}") is None

    asyncio.run(scenario())


def test_delete_many_deduplicates_and_ignores_empty_input() -> None:
    client = _client()

    async def scenario() -> None:
        await client.get_client()
        assert await client.delete_many([]) == 0
        await client.set("dup", "v")
        assert await client.delete_many(["dup", "dup", "dup"]) == 1
        assert await client.get("dup") is None

    asyncio.run(scenario())


def test_delete_many_chunks_large_batches() -> None:
    """A very large flush is split so no single pipeline becomes huge."""
    client = _client()

    async def scenario() -> None:
        fake = _FakeRedis()
        client._client = fake
        keys = [f"k{n}" for n in range(1200)]

        assert await client.delete_many(keys) == 1200
        sizes = [len(pipe.commands) for pipe in fake.pipes]
        assert sum(sizes) == 1200
        assert max(sizes) <= 500, f"batches must be chunked, saw {max(sizes)} keys in one"
        assert len(sizes) == 3

    asyncio.run(scenario())


def test_delete_many_falls_back_to_del_when_unlink_is_missing() -> None:
    """Older Redis servers have no ``UNLINK``; ``DEL`` must be used instead."""

    async def scenario() -> None:
        client = _client()
        fake = _FakeRedisNoUnlink()
        client._client = fake
        assert await client.delete_many(["a"]) == 1
        assert fake.pipes[0].commands == [("delete", "a")]

    asyncio.run(scenario())


def test_delete_many_falls_back_to_memory_on_failure() -> None:
    class _BrokenRedis:
        def pipeline(self, transaction=False):
            raise RuntimeError("connection lost")

    async def scenario() -> None:
        client = _client()
        await client.get_client()
        await client.set("k", "v")
        client._client = _BrokenRedis()
        assert await client.delete_many(["k"]) == 1
        assert client._using_memory()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Flush behaviour unchanged
# ---------------------------------------------------------------------------


def test_flush_user_cache_removes_the_same_keys_and_counts_them() -> None:
    """The end-to-end flush outcome is identical to the per-key version."""
    from app.services.cache_flush_service import flush_user_cache

    client = _client()

    async def scenario() -> None:
        await client.get_client()
        await client.set("conversation:general_agent:5", "history")
        await client.set("conversation:objective_assistant:5", "history")
        await client.set("ai_response:general:5:abc", "cached")
        await client.set("ai_response:general:6:abc", "other user")
        await client.set("rate_limit:1.2.3.4", "counter")
        await client.sadd("proxy_cache:index:5", "proxy_a", "proxy_b")

        counts = await flush_user_cache(client, 5, include_conversations=True)

        assert counts == {
            "conversations_cleared": 2,
            "ai_responses_cleared": 1,
            "proxy_entries_cleared": 2,
        }
        assert await client.get("ai_response:general:5:abc") is None
        assert await client.get("ai_response:general:6:abc") == "other user"
        assert await client.get("rate_limit:1.2.3.4") == "counter"
        assert sorted(await client.smembers("proxy_cache:index:5")) == []

    asyncio.run(scenario())


def test_flush_preserves_conversations_by_default() -> None:
    from app.services.cache_flush_service import flush_user_cache

    client = _client()

    async def scenario() -> None:
        await client.get_client()
        await client.set("conversation:general_agent:5", "history")
        counts = await flush_user_cache(client, 5)
        assert counts["conversations_cleared"] == 0
        assert await client.get("conversation:general_agent:5") == "history"

    asyncio.run(scenario())


def test_flush_never_touches_denylist_or_rate_limit_keys() -> None:
    from app.services.cache_flush_service import flush_user_cache

    client = _client()

    async def scenario() -> None:
        await client.get_client()
        await client.set("denylist:abc123", "1")
        await client.set("rate_limit:5:bucket", "3")
        await flush_user_cache(client, 5, include_conversations=True)
        assert await client.get("denylist:abc123") == "1"
        assert await client.get("rate_limit:5:bucket") == "3"

    asyncio.run(scenario())


def test_flush_on_an_empty_store_is_a_noop() -> None:
    from app.services.cache_flush_service import flush_user_cache

    client = _client()

    async def scenario() -> None:
        await client.get_client()
        assert await flush_user_cache(client, 999) == {
            "conversations_cleared": 0,
            "ai_responses_cleared": 0,
            "proxy_entries_cleared": 0,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 250])
def test_flush_handles_any_number_of_cached_keys(count) -> None:
    """Every cached response must be removed, not just the first batch."""
    from app.services.cache_flush_service import flush_user_cache

    client = _client()

    async def scenario() -> None:
        await client.get_client()
        for n in range(count):
            await client.set(f"ai_response:general:5:key{n}", "cached")
        counts = await flush_user_cache(client, 5)
        assert counts["ai_responses_cleared"] == count
        assert await client.scan_keys("ai_response:general:5:*") == []

    asyncio.run(scenario())
