"""
Command object for requesting a chat completion from the AI provider.

This immutable command encapsulates the user input and the selected
system-prompt ID, keeping the service interface explicit and easy to
validate.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatCompletionCommand:
    """Immutable command carrying the chat completion request data."""

    agent_id: int
    user_message: str

    def __post_init__(self) -> None:
        if self.agent_id <= 0:
            raise ValueError("agent_id must be a positive integer")
        if not self.user_message or not self.user_message.strip():
            raise ValueError("user_message must be a non-empty string")
