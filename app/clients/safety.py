"""Safety filter hooks for AI requests and responses.

The application needs a stable seam to inspect the content that crosses the
AI boundary (both what is sent and what is answered) without scattering
checks through every service.  This module defines the hook interface and a
pluggable pipeline; the default pipeline is a self-documenting pass-through
so enabling the machinery costs nothing until filters are registered.

Filters are intentionally small: ``before_request`` sees the user message
and may rewrite it; ``after_response`` sees the assistant text and may
rewrite it.  Both run inside the router's request lifecycle, so a filter
that blocks must raise (typically :class:`fastapi.HTTPException` or a
typed AI error the app already maps).
"""

from __future__ import annotations

from abc import ABC

from app.core.logging import get_logger

logger = get_logger(__name__)


class SafetyFilter(ABC):  # noqa: B024 - hooks are optional pass-throughs by design
    """Base class for a pre/post AI content filter.

    Subclasses override one or both hooks — neither is abstract because a
    pre-only (or post-only) filter is perfectly valid.  Hooks return the
    (possibly rewritten) text; the default implementation passes content
    through unchanged.
    """

    async def before_request(self, user_message: str) -> str:
        """Inspect/rewrite the user message right before it is sent."""
        return user_message

    async def after_response(self, text: str) -> str:
        """Inspect/rewrite the assistant text right before it is returned."""
        return text


class SafetyFilterPipeline(SafetyFilter):
    """Chain of :class:`SafetyFilter` hooks applied in registration order.

    ``before_request`` runs every filter's pre-hook in order; the output of
    one is the input of the next.  The same happens for ``after_response``.
    An empty pipeline is a pass-through.
    """

    def __init__(self, filters: list[SafetyFilter] | None = None) -> None:
        self._filters = list(filters) if filters else []

    def add(self, filter_: SafetyFilter) -> SafetyFilterPipeline:
        """Append a filter, returning ``self`` for chaining."""
        self._filters.append(filter_)
        return self

    async def before_request(self, user_message: str) -> str:
        current = user_message
        for filter_ in self._filters:
            current = await filter_.before_request(current)
        return current

    async def after_response(self, text: str) -> str:
        current = text
        for filter_ in self._filters:
            current = await filter_.after_response(current)
        return current


class PassThroughSafetyFilter(SafetyFilter):
    """Explicit no-op filter; the default when none is configured.

    Kept as its own class so it can be subclasses/monkeypatched in tests
    without touching the router.
    """

    async def before_request(self, user_message: str) -> str:
        return user_message

    async def after_response(self, text: str) -> str:
        return text


#: The default pipeline (empty = pass-through).  Airouter attaches this when
#: no custom pipeline is supplied, so ``run_ai_request`` always has a safety
#: seam to call without special-casing.
DEFAULT_SAFETY_PIPELINE = SafetyFilterPipeline()
