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

from typing import Any

# Attempt to import the async Redis client.  If the ``redis`` package
# is not installed, set ``redis_async`` to ``None`` so that the
# :class:`RedisClient` can fall back to the in-memory store.
try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover - fallback for environments without redis package
    redis_async = None  # type: ignore[assignment]

import asyncio
import fnmatch
import time

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# How long to wait after a failed connection attempt before trying Redis
# again.  Prevents a dead Redis server from being hammered on every request,
# while still letting a *recovering* Redis be re-discovered on the next call
# (the fallback is never permanently pinned).
_REDIS_RETRY_COOLDOWN_SECONDS = 30.0

# Module-level timestamp of the last attempted Redis connection
# (``time.monotonic``), shared across all client instances.
_last_redis_attempt: float | None = None

# Alias of the builtin ``set`` generic.  mypy resolves a bare ``set`` inside
# a class body to the class's own ``set`` *method*, so return annotations in
# these classes would otherwise fail with "set is not valid as a type".
_Set = set


class InMemoryStore:
    """A minimal in-memory key-value store that mimics a subset of Redis.

    This is used as a fallback when Redis is not available.  It supports:
        - String get/set/delete (with optional TTL — though TTL is not
          actually enforced in this simplified implementation).
        - Sorted-set operations (``zadd``, ``zremrangebyscore``,
          ``zcard``) used by the rate-limiting middleware.
        - Set operations (``sadd``, ``smembers``, ``srem``) used to index
          per-user cache keys.
        - ``keys(pattern)`` and a no-op ``expire`` for cache-flush and
          compatibility.

    Note: This store is **not** shared across processes or workers.
    It is intended for development and testing only.
    """

    def __init__(self) -> None:
        # Simple key→value dictionary for string operations.
        self._data: dict[str, Any] = {}
        # Sorted-set simulation: key → {member: score}.
        self._sorted_sets: dict[str, dict[str, float]] = {}
        # Set simulation: key → {member}.
        self._sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        """Retrieve a value by key, or ``None`` if the key does not exist."""
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value.  The ``ex`` (expiry) parameter is accepted for
        API compatibility but not enforced in this in-memory implementation."""
        self._data[key] = value

    async def delete(self, key: str) -> None:
        """Remove a key from the store (strings, sets and sorted sets).

        No-op if the key does not exist.  Mirrors real Redis ``DEL``, which
        removes every data structure stored under the key.
        """
        self._data.pop(key, None)
        self._sets.pop(key, None)
        self._sorted_sets.pop(key, None)

    async def keys(self, pattern: str) -> list[str]:
        """Return every string key matching a glob pattern."""
        return [key for key in self._data if fnmatch.fnmatchcase(key, pattern)]

    async def expire(self, key: str, seconds: int) -> None:
        """Compatibility no-op (real Redis enforces TTLs; memory does not)."""

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

    async def sadd(self, key: str, *values: str) -> None:
        """Add members to a set, creating it if needed."""
        self._sets.setdefault(key, set()).update(values)

    async def smembers(self, key: str) -> _Set[str]:
        """Return the members of a set (empty when the key does not exist)."""
        return set(self._sets.get(key, set()))

    async def srem(self, key: str, *values: str) -> None:
        """Remove members from a set."""
        members = self._sets.get(key)
        if members:
            members.difference_update(values)


class RedisClient:
    """High-level Redis client with automatic fallback.

    The client lazily connects to Redis on the first operation.  If the
    connection fails (or the ``redis`` package is not installed), it
    transparently switches to an :class:`InMemoryStore`.

    Unlike a naive one-shot fallback, a *failed* Redis is re-tried on a
    later call (subject to a short cooldown) so that a Redis server that
    starts after the app boots is eventually picked up again — the
    in-memory store is never permanently pinned.

    All public methods (``get``, ``set``, ``delete``, ``zadd``,
    ``zremrangebyscore``, ``zcard``) delegate to the underlying client,
    which may be either a real Redis connection or the in-memory store.
    If the real client fails mid-operation, the operation is retried
    against the in-memory store so live traffic is not interrupted.
    """

    # Registry of live instances, so the fallback state can be reset between
    # tests (see :meth:`reset_all_memory_stores`).
    _instances: list["RedisClient"] = []

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialise the client.

        Args:
            settings: Optional :class:`Settings` instance.  If omitted,
                the global singleton is used.
        """
        self.settings = settings or get_settings()
        self._client: Any | None = None
        self._memory_store = InMemoryStore()
        RedisClient._instances.append(self)

    def reset_memory_store(self) -> None:
        """Clear this client's in-memory fallback store.

        Used by the test suite to prevent rate-limiter state recorded by
        one test from leaking into the next.  No-op when the client is
        backed by a real Redis server.
        """
        self._memory_store._data.clear()
        self._memory_store._sorted_sets.clear()
        self._memory_store._sets.clear()

    @classmethod
    def reset_all_memory_stores(cls) -> None:
        """Clear the in-memory fallback store of every live :class:`RedisClient`."""
        for instance in cls._instances:
            instance.reset_memory_store()

    def _using_memory(self) -> bool:
        """Return ``True`` when the current backend is the in-memory store."""
        return self._client is self._memory_store or self._client is None

    async def get_client(self) -> Any:
        """Return the underlying Redis (or in-memory) client.

        The connection is attempted lazily.  When Redis is reachable the
        connection is reused for every subsequent call.  When it is not,
        the in-memory store is used and Redis is re-tried at most once
        per cooldown window (see ``_REDIS_RETRY_COOLDOWN_SECONDS``).
        """
        # A live Redis client is reused as-is.
        if self._client is not None and not self._using_memory():
            return self._client

        # On the in-memory fallback: re-attempt Redis, but respect the
        # cooldown so an unavailable server is not probed on every request.
        global _last_redis_attempt
        tries = None if redis_async is None else _last_redis_attempt
        if redis_async is None:
            self._client = self._memory_store
            return self._client
        now = time.monotonic()
        if tries is not None and now - tries < _REDIS_RETRY_COOLDOWN_SECONDS:
            self._client = self._memory_store
            return self._client

        _last_redis_attempt = now
        try:
            candidate = redis_async.from_url(self.settings.redis_url, decode_responses=True)
            # A short ping ensures the server is reachable so tests do not
            # hang if Redis is unavailable or unresponsive.
            await asyncio.wait_for(candidate.ping(), timeout=0.5)
        except Exception:
            logger.warning("Redis unavailable - falling back to in-memory store")
            self._client = self._memory_store
            return self._client
        self._client = candidate
        return self._client

    async def _run(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Execute an operation, retrying once against memory on a real-client failure.

        ``ex`` is only meaningful for real Redis; the in-memory store uses
        ``set(key, value)``.
        """
        client = await self.get_client()
        if self._using_memory():
            impl = self._memory_store
            if method == "set":
                kwargs = {k: v for k, v in kwargs.items() if k != "ex"}
            return await getattr(impl, method)(*args, **kwargs)
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception:
            logger.exception("Redis operation failed - retrying with in-memory store (%s)", method)
            self._client = self._memory_store
            impl = self._memory_store
            if method == "set":
                kwargs = {k: v for k, v in kwargs.items() if k != "ex"}
            return await getattr(impl, method)(*args, **kwargs)

    async def get(self, key: str) -> str | None:
        """Retrieve a string value by key."""
        return await self._run("get", key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a string value with an optional TTL (``ex`` in seconds)."""
        await self._run("set", key, value, ex=ex)

    async def delete(self, key: str) -> None:
        """Delete a key from the store."""
        await self._run("delete", key)

    async def keys(self, pattern: str) -> list[str]:
        """Return every key matching a glob pattern (e.g. ``conversation:*:5``).

        Intended for the low-frequency per-user cache-flush operation, not for
        hot request paths.
        """
        return await self._run("keys", pattern)

    async def expire(self, key: str, seconds: int) -> None:
        """Set a TTL on a key (no-op for the in-memory fallback)."""
        await self._run("expire", key, seconds)

    async def sadd(self, key: str, *values: str) -> None:
        """Add members to a set, creating it if needed."""
        await self._run("sadd", key, *values)

    async def smembers(self, key: str) -> _Set[str]:
        """Return the members of a set."""
        return await self._run("smembers", key)

    async def srem(self, key: str, *values: str) -> None:
        """Remove members from a set."""
        await self._run("srem", key, *values)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        """Add members to a sorted set."""
        await self._run("zadd", key, mapping)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        """Remove sorted-set members whose scores fall within ``[min_score, max_score]``."""
        await self._run("zremrangebyscore", key, min_score, max_score)

    async def zcard(self, key: str) -> int:
        """Return the cardinality (number of members) of a sorted set."""
        return await self._run("zcard", key)
