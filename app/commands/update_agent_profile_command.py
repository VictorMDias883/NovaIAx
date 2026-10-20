"""
Command object for updating a user's agent personalization profile.

Part of the **Command pattern** used throughout the application.  Every field
is optional so a ``PUT /me/agent`` can patch a single attribute; the service
applies only the provided values.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class UpdateAgentProfileCommand:
    """Immutable command carrying the personalization fields to update.

    Attributes:
        agent_name: New display name of the assistant (2–30 chars).
        tone: Free-form tone descriptor.
        language: Preferred reply language.
        timezone: IANA timezone identifier.
        active_group_slugs: Full replacement list of active group slugs.
        active_sub_agent_slugs: Full replacement list of active sub-agent
            slugs.
        simplified_mode: Whether to produce shorter answers.
    """

    agent_name: str | None = None
    tone: str | None = None
    language: str | None = None
    timezone: str | None = None
    active_group_slugs: list[str] | None = None
    active_sub_agent_slugs: list[str] | None = None
    simplified_mode: bool | None = None
