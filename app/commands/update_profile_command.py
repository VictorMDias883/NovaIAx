"""Command object for a user editing their own profile."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UpdateProfileCommand:
    """Immutable command carrying a self-service profile change.

    The target user is *not* part of the command: self-service edits always
    apply to the authenticated caller, so the acting identity is supplied
    separately by the service layer.

    Attributes:
        full_name: The new display name.
    """

    full_name: str
