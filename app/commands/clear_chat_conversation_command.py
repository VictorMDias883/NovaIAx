"""
Command object for clearing a cached AI chat conversation.

Keeps the Router → Command → Service → Repository layering for the
``DELETE /ai/chat/conversation`` endpoint: the router builds this immutable
command, the service validates the agent against the ``SystemPrompt``
repository and clears the per-user conversation history.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClearChatConversationCommand:
    """Immutable command identifying the conversation to drop."""

    agent_id: int

    def __post_init__(self) -> None:
        if self.agent_id <= 0:
            raise ValueError("agent_id must be a positive integer")
