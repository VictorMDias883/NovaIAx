"""
Redis client wrapper with an in-memory fallback.

This module provides a :class:`RedisClient` that abstracts away the
details of connecting to Redis.  If the ``redis`` package is not
installed or the Redis server is unreachable, the client transparently
falls back to an :class:`InMemoryStore` that mimics the subset of Redis
operations used by the application.

This design allows the application to run in environments without a
Redis server (e.g. local development, CI pipelines) while still
providing the same interface.
"""

import json
from typing import Any

# Attempt to import the async Redis client.  If the ``redis`` package
# is not installed, set ``redis_async`` to ``None`` so that the
# :class:`RedisClient` can fall back to the in-memory store.
try:
    import redis.asyncio as redis_async  # type: ignore
except ImportError:  # pragma: no cover - fallback for environments without redis package
    redis_async = None

from app.core.config import Settings, get_settings


class InMemoryStore:
    """A minimal in-memory key-value store that mimics a subset of Redis.

    This is used as a fallback when Redis is not available.  It supports:
        - String get/set/delete (with optional TTL — though TTL is not
          actually enforced in this simplified implementation).
        - Sorted-set operations (``zadd``, ``zremrangebyscore``,
          ``zcard``) used by the rate-limiting middleware.

    Note: This store is **not** shared across processes or workers.
    It is intended for development and testing only.
    """

    def __init__(self) -> None:
        # Simple key→value dictionary for string operations.
        self._data: dict[str, Any] = {}
        # Sorted-set simulation: key → {member: score}.
        self._sorted_sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        """Retrieve a value by key, or ``None`` if the key does not exist."""
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value.  The ``ex`` (expiry) parameter is accepted for
        API compatibility but not enforced in this in-memory implementation."""
        self._data[key] = value

    async def delete(self, key: str) -> None:
        """Remove a key from the store.  No-op if the key does not exist."""
        self._data.pop(key, None)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        """Add members to a sorted set, or update their scores if they exist."""
        self._sorted_sets[key] = {**self._sorted_sets.get(key, {}), **mapping}

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        """Remove all members from a sorted set whose score falls within
        the given range (inclusive)."""
        items = self._sorted_sets.get(key, {})
        self._sorted_sets[key] = {member: score for member, score in items.items() if not (min_score <= score <= max_score)}

    async def zcard(self, key: str) -> int:
        """Return the number of members in a sorted set."""
        return len(self._sorted_sets.get(key, {}))


class RedisClient:
    """High-level Redis client with automatic fallback.

    The client lazily connects to Redis on the first operation.  If the
    connection fails (or the ``redis`` package is not installed), it
    transparently switches to an :class:`InMemoryStore`.

    All public methods (``get``, ``set``, ``delete``, ``zadd``,
    ``zremrangebyscore``, ``zcard``) delegate to the underlying client,
    which may be either a real Redis connection or the in-memory store.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise the client.

        Args:
            settings: Optional :class:`Settings` instance.  If omitted,
                the global singleton is used.
        """
        self.settings = settings or get_settings()
        self._client: Any | None = None
        self._memory_store = InMemoryStore()

    async def get_client(self) -> Any:
        """Return the underlying Redis (or in-memory) client.

        On the first call, this method attempts to create a real Redis
        connection and ping it.  If that fails for any reason, it falls
        back to the :class:`InMemoryStore`.  The result is cached so
        subsequent calls do not repeat the connection logic.
        """
        if self._client is None:
            if redis_async is None:
                # The redis package is not installed — use in-memory store.
                self._client = self._memory_store
            else:
                try:
                    self._client = redis_async.from_url(self.settings.redis_url, decode_responses=True)
                    await self._client.ping()
                except Exception:
                    # Redis is unreachable — fall back to in-memory store.
                    self._client = self._memory_store
        return self._client

    async def get(self, key: str) -> str | None:
        """Retrieve a string value by key."""
        client = await self.get_client()
        return await client.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a string value with an optional TTL (``ex`` in seconds).

        The ``hasattr`` check accommodates the :class:`InMemoryStore`,
        which does not support the ``ex`` parameter.
        """
        client = await self.get_client()
        if hasattr(client, "set"):
            await client.set(key, value, ex=ex)
        else:
            await client.set(key, value)

    async def delete(self, key: str) -> None:
        """Delete a key from the store."""
        client = await self.get_client()
        await client.delete(key)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        """Add members to a sorted set."""
        client = await self.get_client()
        await client.zadd(key, mapping)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        """Remove sorted-set members whose scores fall within ``[min_score, max_score]``."""
        client = await self.get_client()
        await client.zremrangebyscore(key, min_score, max_score)

    async def zcard(self, key: str) -> int:
        """Return the cardinality (number of members) of a sorted set."""
        client = await self.get_client()
        return await client.zcard(key)
