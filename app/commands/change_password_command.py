"""Command object for a user rotating their own password."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChangePasswordCommand:
    """Immutable command carrying a self-service password change.

    The target user is *not* part of the command: self-service changes
    always apply to the authenticated caller, so the acting identity is
    supplied separately by the service layer.

    Attributes:
        current_password: The account's existing plaintext password,
            verified against the stored hash before anything is written.
        new_password: The replacement plaintext password (already
            complexity-checked by the request schema).
    """

    current_password: str
    new_password: str
