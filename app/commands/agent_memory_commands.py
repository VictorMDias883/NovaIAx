"""
Command objects for the per-user memory endpoints.

Part of the **Command pattern** used throughout the application.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CreateUserMemoryCommand:
    """Immutable command carrying an explicit user-saved memory.

    Attributes:
        key: Stable identifier, unique per user (the ``auto_`` prefix is
            reserved for auto-learned memories and rejected here).
        value: The remembered fact (1–500 chars).
    """

    key: str
    value: str


@dataclass(frozen=True)
class UpdateUserMemoryCommand:
    """Immutable command carrying the memory fields to patch.

    At least one of ``value`` or ``enabled`` must be provided (validated by
    the schema layer).
    """

    value: str | None = None
    enabled: bool | None = None
