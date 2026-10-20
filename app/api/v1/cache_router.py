"""API v1 cache-management endpoints.

Provides a per-user cache-flush endpoint: clears every cached key belonging
to the authenticated user (their AI chat-completion responses and their
reverse-proxy response-cache entries) without touching any other user's data
or the shared rate-limit/denylist state.  Conversation histories are preserved
unless the caller explicitly opts in with ``include_conversations=true``.

Only real database users may call this endpoint — API-key identities get a
403 via :func:`require_db_user` (their cache is keyed by key-hash, not a user
id, so there is nothing user-scoped to flush).
"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_redis_client, require_db_user
from app.cache.redis_client import RedisClient
from app.schemas.cache_schemas import CacheFlushResponse
from app.services.cache_flush_service import flush_user_cache
from app.utils.request import current_user_id

router = APIRouter(prefix="/cache", tags=["cache"])


@router.post("/flush", response_model=CacheFlushResponse)
async def flush_cache(
    include_conversations: bool = Query(
        default=False,
        description=(
            "Also delete the caller's AI conversation histories "
            "(`conversation:*:<user_id>`). Off by default so a routine flush "
            "does not wipe the assistant's memory of the current thread."
        ),
    ),
    current_user: dict = Depends(require_db_user),
    redis_client: RedisClient = Depends(get_redis_client),
) -> CacheFlushResponse:
    """Clear cached data belonging to the authenticated user.

    Deletes their user-scoped AI chat responses and their reverse-proxy
    response cache entries.  Their conversation histories survive unless
    ``include_conversations=true`` is passed.  The operation is scoped to the
    caller — no other user's keys and no global state (rate-limit counters,
    token denylist) are touched.

    Returns:
        ``{"status": "ok", "user_id": ..., "cleared": {...}}`` with
        per-category deletion counts.  ``cleared.conversations_cleared`` is
        ``0`` whenever ``include_conversations`` is false.

    Raises:
        HTTPException(401): If the request is not authenticated.
        HTTPException(403): If an API-key identity calls the endpoint.
    """
    user_id = current_user_id(current_user)
    cleared = await flush_user_cache(
        redis_client,
        user_id,
        include_conversations=include_conversations,
    )
    return CacheFlushResponse(
        status="ok",
        user_id=user_id,
        cleared=cleared,
    )
