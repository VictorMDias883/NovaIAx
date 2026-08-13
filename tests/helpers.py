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