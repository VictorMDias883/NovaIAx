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

#: Rough token budget for the cached history.  Keeping the history well
#: below the provider's per-minute token limit prevents ``429``s caused by
#: large backlogs and stops the oldest (but still relevant) turns from being
#: silently evicted by a single huge message.  Tokens are approximated as
#: ``chars / 4`` which is close enough for a *limiting* heuristic.
MAX_HISTORY_TOKENS = 6000

#: Estimated tokens added per message on top of its content.
_MESSAGE_OVERHEAD_TOKENS = 4
#: Approximate characters that fit into a single token.
_CHARS_PER_TOKEN = 4

_ALLOWED_ROLES = {"user", "assistant"}
#: Namespace prefix for all conversation-history keys (shared with the
#: per-user cache-flush operation, which matches ``{KEY_PREFIX}*:{user_id}``).
KEY_PREFIX = "conversation:"


def _estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Approximate the token count of a message list (chars/4 + overhead)."""
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD_TOKENS + len(message.get("content", "")) // _CHARS_PER_TOKEN
    return total


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
        return f"{KEY_PREFIX}{agent}:{user_id}"

    @staticmethod
    def key_pattern(user_id: int) -> str:
        """Return a glob matching every conversation key belonging to a user."""
        return f"{KEY_PREFIX}*:{user_id}"

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
        """Persist the conversation history, trimming the oldest messages.

        Trimming drops complete oldest turns (two messages at a time to keep
        ``user``/``assistant`` alternation intact) until the history fits both
        the message cap (:data:`MAX_HISTORY_MESSAGES`) and the token budget
        (:data:`MAX_HISTORY_TOKENS`).  The latest turn is always preserved, so
        the assistant never loses the most recent exchange.
        """
        trimmed = list(messages)
        while len(trimmed) > 2 and (
            len(trimmed) > MAX_HISTORY_MESSAGES or _estimate_tokens(trimmed) > MAX_HISTORY_TOKENS
        ):
            trimmed = trimmed[2:]
        await self.client.set(self._key(agent, user_id), json.dumps(trimmed, ensure_ascii=False))

    async def clear(self, agent: str, user_id: int) -> None:
        """Delete the entire conversation history for an agent and user."""
        await self.client.delete(self._key(agent, user_id))
