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

#: How many keys are sent per pipelined ``UNLINK``/``DEL`` batch.  Large
#: enough that a per-user flush needs a handful of round-trips instead of
#: hundreds, small enough to keep each pipeline's reply payload modest.
_DELETE_PIPELINE_CHUNK = 500


class InMemoryStore:
    """A minimal in-memory key-value store that mimics a subset of Redis.

    This is used as a fallback when Redis is not available.  It supports:
        - String get/set/delete with **enforced TTLs**, matching Redis
          semantics rather than storing values forever.
        - Sorted-set operations (``zadd``, ``zremrangebyscore``,
          ``zcard``) used by the rate-limiting middleware.
        - Set operations (``sadd``, ``smembers``, ``srem``) used to index
          per-user cache keys.
        - ``keys(pattern)`` and ``expire`` for cache-flush and
          compatibility.

    Expiry is evaluated lazily: a key is treated as absent the moment it is
    read after its deadline, and dead entries are physically removed on the
    next operation.  Redis expires keys eagerly in the background; the
    observable behaviour (``get`` returns ``None``) is the same, and this
    store is not shared between processes, so a periodic sweeper would only
    add overhead.

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
        # Key → absolute ``time.monotonic()`` deadline.  A key absent from
        # this mapping never expires.
        self._expiry: dict[str, float] = {}

    def _purge_expired(self) -> None:
        """Drop every key whose TTL has elapsed."""
        if not self._expiry:
            return
        now = time.monotonic()
        for key in [key for key, deadline in self._expiry.items() if deadline <= now]:
            self._data.pop(key, None)
            self._sets.pop(key, None)
            self._sorted_sets.pop(key, None)
            self._expiry.pop(key, None)

    async def get(self, key: str) -> str | None:
        """Retrieve a value by key, or ``None`` if the key does not exist or expired."""
        self._purge_expired()
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store a value, expiring it after ``ex`` seconds when given.

        A non-positive or missing ``ex`` clears any previous deadline, so
        ``set(k, v)`` after ``set(k, v, ex=60)`` makes the key permanent —
        the same behaviour Redis has when ``SET`` is used without ``EX``.
        """
        self._purge_expired()
        self._data[key] = value
        if ex is not None and ex > 0:
            self._expiry[key] = time.monotonic() + ex
        else:
            self._expiry.pop(key, None)

    async def delete(self, key: str) -> None:
        """Remove a key from the store (strings, sets and sorted sets).

        No-op if the key does not exist.  Mirrors real Redis ``DEL``, which
        removes every data structure stored under the key.
        """
        self._data.pop(key, None)
        self._sets.pop(key, None)
        self._sorted_sets.pop(key, None)
        self._expiry.pop(key, None)

    async def keys(self, pattern: str) -> list[str]:
        """Return every string key matching a glob pattern."""
        self._purge_expired()
        return [key for key in self._data if fnmatch.fnmatchcase(key, pattern)]

    async def expire(self, key: str, seconds: int) -> None:
        """Set a TTL on an existing key (no-op when it does not exist).

        Mirrors Redis, where ``EXPIRE`` on a missing key is a no-op rather
        than an error.
        """
        self._purge_expired()
        if key in self._data and seconds > 0:
            self._expiry[key] = time.monotonic() + seconds
        else:
            self._expiry.pop(key, None)

    async def incrby(self, key: str, amount: int = 1) -> int:
        """Add ``amount`` to a counter, creating it at ``amount`` if missing.

        Values are stored as strings so :meth:`get` keeps returning ``str``
        while still always holding a parseable integer (mirroring Redis' own
        string-based counters).
        """
        self._purge_expired()
        try:
            current = int(self._data.get(key, 0))
        except (TypeError, ValueError):
            current = 0
        new_value = current + amount
        self._data[key] = str(new_value)
        return new_value

    async def ttl(self, key: str) -> int:
        """Return the remaining TTL of a key in seconds (Redis semantics).

        ``-2`` when the key does not exist, ``-1`` when it has no expiry,
        otherwise the whole seconds left.  Used to size a quota window so a
        counter and its reset deadline stay in sync.
        """
        self._purge_expired()
        if key not in self._data:
            return -2
        deadline = self._expiry.get(key)
        if deadline is None:
            return -1
        return max(int(deadline - time.monotonic()), 0)

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
        self._memory_store._expiry.clear()

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

        ``ex`` is forwarded to both backends: real Redis turns it into a
        ``SET ... EX``, and :class:`InMemoryStore` now enforces it too.  It
        used to be dropped for the memory path, which meant that whenever
        Redis was unreachable the cached conversations and responses never
        expired at all.
        """
        client = await self.get_client()
        if self._using_memory():
            return await getattr(self._memory_store, method)(*args, **kwargs)
        try:
            return await getattr(client, method)(*args, **kwargs)
        except Exception:
            logger.exception("Redis operation failed - retrying with in-memory store (%s)", method)
            self._client = self._memory_store
            return await getattr(self._memory_store, method)(*args, **kwargs)

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

        .. warning::
           Backed by Redis ``KEYS``, which is O(N) over the *whole*
           keyspace and blocks the server while it runs.  Prefer
           :meth:`scan_keys` — this method exists only where a glob over
           string values is genuinely needed, and never on a hot path.
        """
        return await self._run("keys", pattern)

    async def scan_keys(self, pattern: str, count: int = 500) -> list[str]:
        """Return every key matching a glob pattern, using Redis ``SCAN``.

        ``KEYS`` forces Redis to walk the entire keyspace in one blocking
        call; on a busy instance that stalls *every* other client, so even a
        per-user cache flush becomes a self-inflicted outage.  ``SCAN``
        returns a cursor and a bounded batch of matches per round trip,
        leaving the server responsive in between.

        Args:
            pattern: A glob pattern, e.g. ``ai_response:*:5:*``.
            count: Hint for how many keys to examine per round trip.

        Returns:
            The matching keys.  As with any iteration over a live keyspace,
            keys added or removed while scanning may or may not appear.
        """
        client = await self.get_client()
        if self._using_memory():
            return await self._memory_store.keys(pattern)

        try:
            found: list[str] = []
            async for key in client.scan_iter(match=pattern, count=count):
                found.append(key)
            return found
        except Exception:
            logger.exception("Redis SCAN failed - falling back to an in-memory scan (%s)", pattern)
            self._client = self._memory_store
            return await self._memory_store.keys(pattern)

    async def delete_many(self, keys: list[str]) -> int:
        """Delete several keys in a single (pipelined) round trip.

        Deleting keys one at a time costs one network round trip per key,
        which is exactly the wrong shape for a cache flush: a user with a few
        hundred cached responses would wait on a few hundred sequential
        commands.  Batching them into one pipeline turns that into a single
        exchange, and ``UNLINK`` (Redis 4+) reclaims the memory in a
        background thread instead of blocking the event loop on ``DEL``.

        Falls back to ``DEL`` on servers without ``UNLINK`` and to the
        in-memory store on any connection failure.

        Args:
            keys: The keys to delete.  Duplicates are collapsed and the
                list is chunked to stay well below argument limits.

        Returns:
            The number of keys actually removed.
        """
        unique = list(dict.fromkeys(keys))
        if not unique:
            return 0

        client = await self.get_client()
        if self._using_memory():
            for key in unique:
                await self._memory_store.delete(key)
            return len(unique)

        try:
            return await self._piped_delete(client, unique)
        except Exception:
            logger.exception("Redis pipelined delete failed - retrying with in-memory store")
            self._client = self._memory_store
            for key in unique:
                await self._memory_store.delete(key)
            return len(unique)

    @staticmethod
    async def _piped_delete(client: Any, keys: list[str]) -> int:
        """Run chunked ``UNLINK``/``DEL`` pipelines and return the total removed."""
        total = 0
        command = "unlink" if hasattr(client, "unlink") else "delete"
        for start in range(0, len(keys), _DELETE_PIPELINE_CHUNK):
            chunk = keys[start : start + _DELETE_PIPELINE_CHUNK]
            pipe = client.pipeline(transaction=False)
            for key in chunk:
                getattr(pipe, command)(key)
            results = await pipe.execute()
            total += sum(int(result or 0) for result in results)
        return total

    async def expire(self, key: str, seconds: int) -> None:
        """Set a TTL on a key (no-op for the in-memory fallback)."""
        await self._run("expire", key, seconds)

    async def incrby(self, key: str, amount: int = 1) -> int:
        """Add ``amount`` to a counter, creating it at ``amount`` if missing."""
        return await self._run("incrby", key, amount)

    async def ttl(self, key: str) -> int:
        """Return the remaining TTL of a key in seconds (Redis semantics)."""
        return await self._run("ttl", key)

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
