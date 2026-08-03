import json
from typing import Any

try:
    import redis.asyncio as redis_async  # type: ignore
except ImportError:  # pragma: no cover - fallback for environments without redis package
    redis_async = None

from gateway.app.core.config import Settings, get_settings


class InMemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._sorted_sets: dict[str, dict[str, float]] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._sorted_sets[key] = {**self._sorted_sets.get(key, {}), **mapping}

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        items = self._sorted_sets.get(key, {})
        self._sorted_sets[key] = {member: score for member, score in items.items() if not (min_score <= score <= max_score)}

    async def zcard(self, key: str) -> int:
        return len(self._sorted_sets.get(key, {}))


class RedisClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any | None = None
        self._memory_store = InMemoryStore()

    async def get_client(self) -> Any:
        if self._client is None:
            if redis_async is None:
                self._client = self._memory_store
            else:
                try:
                    self._client = redis_async.from_url(self.settings.redis_url, decode_responses=True)
                    await self._client.ping()
                except Exception:
                    self._client = self._memory_store
        return self._client

    async def get(self, key: str) -> str | None:
        client = await self.get_client()
        return await client.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        client = await self.get_client()
        if hasattr(client, "set"):
            await client.set(key, value, ex=ex)
        else:
            await client.set(key, value)

    async def delete(self, key: str) -> None:
        client = await self.get_client()
        await client.delete(key)

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        client = await self.get_client()
        await client.zadd(key, mapping)

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> None:
        client = await self.get_client()
        await client.zremrangebyscore(key, min_score, max_score)

    async def zcard(self, key: str) -> int:
        client = await self.get_client()
        return await client.zcard(key)
