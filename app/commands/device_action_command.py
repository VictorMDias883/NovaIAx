"""
Command object for the device-action agent.

This command carries the user's natural-language request for a device
action, keeping the service boundary explicit and reusable.  It deliberately
holds no action information: the action itself is produced and validated by
the service from the model's reply, never supplied by the caller.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceActionCommand:
    """Immutable command containing the user's message for the device agent."""

    user_message: str

    def __post_init__(self) -> None:
        if not self.user_message or not self.user_message.strip():
            raise ValueError("user_message must be a non-empty string")
