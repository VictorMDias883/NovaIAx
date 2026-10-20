"""Shared test helpers."""

from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.core.config import Settings


def make_conversation_cache() -> ConversationCache:
    """Return a cache backed by an in-memory store (no real Redis).

    The client points at an unreachable local address so the connection
    fails immediately and the in-memory fallback is used.  This keeps
    tests deterministic and independent of the environment.
    """
    settings = Settings(redis_url="redis://127.0.0.1:1/0")
    return ConversationCache(RedisClient(settings))


def force_memory_redis(monkeypatch) -> RedisClient:
    """Point the DI container at one shared in-memory :class:`RedisClient`.

    Returns the client so a test can seed and inspect it directly.

    Both halves are required for the patch to take effect:

    * ``app.api.deps.RedisClient`` is replaced so any *newly built* client
      falls back to the in-memory store;
    * the process-wide ``_persistent_redis_client`` singleton is cleared,
      because :func:`app.api.deps.get_redis_client` returns the cached
      instance whenever one already exists — otherwise the patch would be a
      silent no-op and a test would read back the *previous* test's store
      instead of the one it just seeded.

    ``monkeypatch`` restores the previous singleton on teardown, so the
    isolation holds for every test in a session regardless of ordering.
    """
    import app.api.deps as deps

    shared = RedisClient(Settings(redis_url="redis://127.0.0.1:1/0"))
    monkeypatch.setattr(deps, "RedisClient", lambda *a, **k: shared)
    monkeypatch.setattr(deps, "_persistent_redis_client", None, raising=False)
    return shared
