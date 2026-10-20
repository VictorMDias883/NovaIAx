"""
Process-wide snapshot of the per-plan ceiling overrides.

:data:`app.clients.plan_limits.PLAN_LIMITS` is the compiled-in default for
every subscription ceiling.  An operator may override individual ceilings from
the admin panel (``plan_limit_overrides`` table); those overrides must reach
``get_plan_limits`` — which is **synchronous** and called from many hot paths —
without a database round-trip on every call.

This module holds the last loaded snapshot in memory.  Callers with a database
session refresh it (see
:func:`app.services.plan_limit_override_service.refresh_plan_limit_overrides`)
at most once per TTL; the writer pushes the fresh snapshot immediately after a
change, so a toggle applies on the very next request in the same worker while
other workers converge within the TTL.

The snapshot is intentionally a plain dict so this module has no import cycle
with :mod:`app.clients.plan_limits` (which imports the singleton at the end of
:func:`get_plan_limits`).
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from app.clients.plan_limits import PlanLimits

#: Fields an override row may set.  Anything else is ignored.
OVERRIDABLE_FIELDS = (
    "daily_messages",
    "daily_tokens",
    "daily_device_actions",
    "max_groups",
    "max_sub_agents",
    "models",
    "features",
)


class PlanLimitOverrides:
    """In-memory snapshot of the plan ceiling overrides."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded_at: float = 0.0

    def reset(self) -> None:
        """Drop the snapshot (used by tests between cases)."""
        self._rows = {}
        self._loaded_at = 0.0

    @property
    def rows(self) -> dict[str, dict[str, Any]]:
        """The snapshot keyed by plan value."""
        return self._rows

    def set_rows(self, rows: dict[str, dict[str, Any]]) -> None:
        """Replace the snapshot and stamp the load time."""
        self._rows = {plan: dict(values) for plan, values in rows.items()}
        self._loaded_at = time.monotonic()

    def is_stale(self, ttl_seconds: float) -> bool:
        """True when the snapshot is older than ``ttl_seconds``."""
        return (time.monotonic() - self._loaded_at) >= ttl_seconds

    def apply(self, limits: PlanLimits) -> PlanLimits:
        """Return ``limits`` with the plan's overrides folded in (if any)."""
        override = self._rows.get(limits.plan.value)
        if not override:
            return limits
        clean = {
            key: value
            for key, value in override.items()
            if key in OVERRIDABLE_FIELDS and value is not None
        }
        if not clean:
            return limits
        return replace(limits, **clean)


#: Process-wide singleton read by :func:`app.clients.plan_limits.get_plan_limits`.
plan_limit_overrides = PlanLimitOverrides()
