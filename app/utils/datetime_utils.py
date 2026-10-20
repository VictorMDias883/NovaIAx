"""
Datetime normalisation helpers shared across services.

Persisted ``DateTime(timezone=True)`` columns are timezone-aware on
PostgreSQL but come back **naive** from SQLite, which is what the test
suite runs against.  Comparing a naive datetime with ``datetime.now(UTC)``
raises ``TypeError``, so every comparison has to normalise first.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["ensure_aware"]


def ensure_aware(value: datetime) -> datetime:
    """Return ``value`` with a UTC timezone, assuming UTC when naive.

    Args:
        value: A datetime that may or may not carry timezone information.

    Returns:
        The same instant as ``value``, guaranteed to be timezone-aware.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
