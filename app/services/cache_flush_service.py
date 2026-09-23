"""Per-user cache flush.

Clears every Redis key that belongs to a *single* user (never the whole
store).  Currently that is:

    - conversation histories: ``conversation:<agent>:<user_id>`` (all agents),
    - reverse-proxy response cache entries indexed under
      ``proxy_cache:index:<user_id>``, whose keys embed a hash of the
      caller's identity and therefore cannot be matched by pattern.

Rate-limit counters, token-denylist entries and the ``ai_response`` cache
are intentionally *not* touched: they are keyed by client IP, by ``jti`` or
by prompt digest — none of which is user-owned data.
"""

from app.api.v1.proxy import PROXY_CACHE_INDEX_PREFIX
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient


async def flush_user_cache(redis_client: RedisClient, user_id: int) -> dict[str, int]:
    """Delete every cached key belonging to ``user_id``.

    Args:
        redis_client: The shared :class:`RedisClient`.
        user_id: The database id of the user whose cache should be cleared.

    Returns:
        A ``{"conversations_cleared": int, "proxy_entries_cleared": int}``
        count of the deleted keys.
    """
    # Conversation histories for every assistant the user has talked to.
    conversation_keys = await redis_client.keys(ConversationCache.key_pattern(user_id))
    for key in conversation_keys:
        await redis_client.delete(key)

    # Reverse-proxy response cache entries, tracked via the user's index set.
    index_key = f"{PROXY_CACHE_INDEX_PREFIX}{user_id}"
    proxy_keys = await redis_client.smembers(index_key)
    for key in proxy_keys:
        await redis_client.delete(key)
    await redis_client.delete(index_key)

    return {
        "conversations_cleared": len(conversation_keys),
        "proxy_entries_cleared": len(proxy_keys),
    }
