"""API v1 cache-management endpoints.

Provides a per-user cache-flush endpoint: clears every cached key belonging
to the authenticated user (conversation histories and their reverse-proxy
response-cache entries), without touching any other user's data or the shared
rate-limit/denylist state.

Only real database users may call this endpoint — API-key identities get a
403 via :func:`require_db_user` (their cache is keyed by key-hash, not a user
id, so there is nothing user-scoped to flush).
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_redis_client, require_db_user
from app.cache.redis_client import RedisClient
from app.services.cache_flush_service import flush_user_cache

router = APIRouter(prefix="/cache", tags=["cache"])


@router.post("/flush")
async def flush_cache(
    current_user: dict = Depends(require_db_user),
    redis_client: RedisClient = Depends(get_redis_client),
) -> dict[str, object]:
    """Clear all cached data belonging to the authenticated user.

    Deletes their AI conversation histories and their reverse-proxy response
    cache entries.  The operation is scoped to the caller — no other user's
    keys and no global state (rate-limit counters, token denylist, shared
    prompt caches) are touched.

    Returns:
        ``{"status": "ok", "cleared": {...}}`` with per-category deletion
        counts.

    Raises:
        HTTPException(401): If the request is not authenticated.
        HTTPException(403): If an API-key identity calls the endpoint.
    """
    user_id = int(current_user["id"])
    cleared = await flush_user_cache(redis_client, user_id)
    return {"status": "ok", "user_id": user_id, "cleared": cleared}
