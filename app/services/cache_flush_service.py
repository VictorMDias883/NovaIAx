"""Per-user cache flush.

Clears every Redis key that belongs to a *single* user (never the whole
store).  Currently that is:

    - conversation histories: ``conversation:<agent>:<user_id>`` (all agents),
      **only** when the caller opts in via ``include_conversations`` — a cache
      flush is meant to drop derived state such as cached AI responses, and
      silently destroying a multi-turn conversation would lose the assistant's
      memory of the thread the user is in the middle of;
    - AI chat-completion responses: ``ai_response:<agent>:<user_id>:<digest>``
      (legacy response-cache keys, no longer written by ``/ai/chat``);
    - reverse-proxy response cache entries indexed under
      ``proxy_cache:index:<user_id>``, whose keys embed a hash of the
      caller's identity and therefore cannot be matched by pattern.

Rate-limit counters and token-denylist entries are intentionally *not*
touched: they are keyed by client IP and by ``jti``, neither of which is
user-owned data.

Both the discovery and the deletion are batched: keys are found with Redis
``SCAN`` (never ``KEYS``, which walks the whole keyspace and blocks every
other client while it does) and removed through one pipelined
``UNLINK``/``DEL`` per batch instead of one round trip per key.
"""

from app.api.v1.proxy import PROXY_CACHE_INDEX_PREFIX
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient

#: Prefix of the per-user AI chat-completion response cache keys.
AI_RESPONSE_PREFIX = "ai_response:"


async def flush_user_cache(
    redis_client: RedisClient,
    user_id: int,
    include_conversations: bool = False,
) -> dict[str, int]:
    """Delete the cached keys belonging to ``user_id``.

    Args:
        redis_client: The shared :class:`RedisClient`.
        user_id: The database id of the user whose cache should be cleared.
        include_conversations: Whether conversation histories
            (``conversation:*:<user_id>``) should be deleted as well.  Off by
            default, so a routine flush preserves the conversation thread.

    Returns:
        A ``{"conversations_cleared": int, "ai_responses_cleared": int,
        "proxy_entries_cleared": int}`` count of the deleted keys.
        ``conversations_cleared`` is ``0`` when ``include_conversations`` is
        false, whatever the stored history was.
    """
    # Conversation histories for every assistant the user has talked to.  Only
    # removed on explicit opt-in — see the module docstring.
    conversation_keys: list[str] = []
    if include_conversations:
        conversation_keys = await redis_client.scan_keys(ConversationCache.key_pattern(user_id))
        await redis_client.delete_many(conversation_keys)

    # AI chat-completion responses for the user (user-scoped cache keys).
    ai_response_keys = await redis_client.scan_keys(f"{AI_RESPONSE_PREFIX}*:{user_id}:*")
    await redis_client.delete_many(ai_response_keys)

    # Reverse-proxy response cache entries, tracked via the user's index set.
    index_key = f"{PROXY_CACHE_INDEX_PREFIX}{user_id}"
    proxy_keys = list(await redis_client.smembers(index_key))
    # The index set itself is deleted together with the entries it points at.
    await redis_client.delete_many([*proxy_keys, index_key])

    return {
        "conversations_cleared": len(conversation_keys),
        "ai_responses_cleared": len(ai_response_keys),
        "proxy_entries_cleared": len(proxy_keys),
    }
