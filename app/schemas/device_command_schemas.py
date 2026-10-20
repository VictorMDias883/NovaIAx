"""
Request and response bodies for the device-command agent.

These live apart from :mod:`app.schemas.device_action_schemas`, which owns
the action whitelist itself: this module is only the transport envelope.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.device_action_schemas import DeviceAction


class DeviceCommandRequest(BaseModel):
    """Body of ``POST /api/v1/agents/device/command``."""

    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(
        min_length=1,
        max_length=5000,
        description="The user's request, in their own words.",
    )


class DeviceCommandResponse(BaseModel):
    """Reply plus the single action the client may carry out.

    ``action`` is ``None`` when the message needed no action or the model's
    output fell outside the whitelist.  ``reply`` is always present.
    """

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(description="Natural-language answer for the user.")
    message_id: str = Field(
        description=(
            "Stable identifier of this turn, persisted server-side. Echo it "
            "back in `POST /api/v1/ai/feedback` to report the answer."
        )
    )
    action: DeviceAction | None = Field(
        default=None,
        description=(
            "One validated, whitelisted action, or null. The backend never "
            "executes it. When present the client must respect "
            "`requires_confirmation` and `prepares_only`."
        ),
    )
