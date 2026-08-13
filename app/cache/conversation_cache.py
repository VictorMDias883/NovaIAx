"""
Conversation history cache backed by Redis (with in-memory fallback).

Each assistant keeps a logical conversation per user, stored as a JSON
list of ``{"role": ..., "content": ...}`` messages under a single key.
On every turn the service reads the previous messages, appends the new
user message and the assistant reply, and writes the list back, so the
next turn can send the full history to the AI provider and preserve
continuity and memory.
"""

import json
from typing import Any

from app.cache.redis_client import RedisClient

#: Maximum number of messages kept per conversation.  Older messages are
#: trimmed to avoid unbounded growth of the cached history.
MAX_HISTORY_MESSAGES = 50

_ALLOWED_ROLES = {"user", "assistant"}


class ConversationCache:
    """Store and retrieve message history for assistant conversations."""

    def __init__(self, client: RedisClient | None = None) -> None:
        """Initialise the cache wrapper.

        Args:
            client: A :class:`RedisClient` instance.  If omitted, a new
                client (with its own in-memory fallback) is created.
        """
        self.client = client or RedisClient()

    @staticmethod
    def _key(agent: str, user_id: int) -> str:
        """Return the cache key for a given agent and user."""
        return f"conversation:{agent}:{user_id}"

    async def get_messages(self, agent: str, user_id: int) -> list[dict[str, str]]:
        """Return the stored history for a conversation, or ``[]`` if empty.

        Malformed entries are filtered out so the AI never receives
        invalid messages.
        """
        raw = await self.client.get(self._key(agent, user_id))
        if not raw:
            return []
        try:
            data: Any = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [
            {"role": str(item["role"]), "content": str(item["content"])}
            for item in data
            if isinstance(item, dict)
            and item.get("role") in _ALLOWED_ROLES
            and isinstance(item.get("content"), str)
            and item["content"].strip()
        ]

    async def save(self, agent: str, user_id: int, messages: list[dict[str, str]]) -> None:
        """Persist the conversation history, trimming the oldest messages."""
        trimmed = messages[-MAX_HISTORY_MESSAGES:]
        await self.client.set(self._key(agent, user_id), json.dumps(trimmed, ensure_ascii=False))

    async def clear(self, agent: str, user_id: int) -> None:
        """Delete the entire conversation history for an agent and user."""
        await self.client.delete(self._key(agent, user_id))