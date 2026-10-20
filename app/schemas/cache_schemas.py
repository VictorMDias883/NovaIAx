"""Pydantic response schemas for the cache-management endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class CacheFlushBreakdown(BaseModel):
    """Per-category counts of the keys removed by a cache flush.

    ``conversations_cleared`` is always ``0`` unless the caller opted in with
    ``include_conversations=true``.
    """

    conversations_cleared: int
    ai_responses_cleared: int
    proxy_entries_cleared: int


class CacheFlushResponse(BaseModel):
    """Result of flushing the caller's user-scoped cache.

    The flush never touches other users' keys nor global state such as
    rate-limit counters or the token denylist.
    """

    status: str
    user_id: int
    cleared: CacheFlushBreakdown
