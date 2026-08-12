"""
Command object for the specialized objective-creation assistant.

This command carries the user's natural-language message for the
assistant workflow, keeping the service boundary explicit and reusable.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ObjectiveAssistantCommand:
    """Immutable command containing the user's message for the assistant."""

    user_message: str

    def __post_init__(self) -> None:
        if not self.user_message or not self.user_message.strip():
            raise ValueError("user_message must be a non-empty string")
