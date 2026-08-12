"""
Command object for the general agent.

This command carries the user's natural-language message for the general
agent workflow, keeping the service boundary explicit and reusable.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GeneralAgentCommand:
    """Immutable command containing the user's message for the general agent."""

    user_message: str

    def __post_init__(self) -> None:
        if not self.user_message or not self.user_message.strip():
            raise ValueError("user_message must be a non-empty string")
