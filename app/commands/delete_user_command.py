"""Command object for deleting a user account."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DeleteUserCommand:
    """Immutable command carrying the ID of the user to delete."""

    target_id: int
