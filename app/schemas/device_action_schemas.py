"""
Strict schema whitelist for the actions the device agent may propose.

Every action the model is allowed to emit is declared here exactly once.  The
model's raw JSON is validated against :data:`DeviceActionAdapter`, so a type,
a parameter name, a parameter range or an extra key that is not declared
below is **rejected** — the whole action is dropped and the user simply gets
the reply (see
:mod:`app.services.device_action_service`).  There is no permissive fallback
shape: "close enough" is the same as "not whitelisted".

Google Play compliance
----------------------
Two rules drive the shape of this module:

1. **Nothing happens without the user's final tap.**  The backend never
   executes anything; it returns a proposal and the client decides.  For the
   three actions in :data:`REQUIRES_CONFIRMATION` the client *must* show a
   confirmation prompt first.
2. **No action sends a message or places a call on its own.**  :class:`DialAction`
   and :class:`ComposeMessageAction` are *preparations*: they resolve a
   contact and fill in a draft, and the user still presses send/call inside
   the target app.  Both set :attr:`prepares_only` to ``True`` so the client
   can label them correctly instead of announcing "message sent".
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

#: Keys whose value is decided by this module and must never be taken from
#: the model's reply.
_SERVER_POLICY_KEYS = ("requires_confirmation", "prepares_only")


def _strip_model_supplied_policy(value: Any) -> Any:
    """Remove the server-policy keys from a model-supplied action.

    Confirmation and prepare-only-ness are **server-side policy**
    (:data:`REQUIRES_CONFIRMATION`, :data:`PREPARES_ONLY`), not model
    decisions.  Stripping the keys before validation means a prompt-injected
    ``"requires_confirmation": false`` cannot downgrade
    :class:`ComposeMessageAction`, :class:`DialAction` or
    :class:`MarkDayCompletedAction` to auto-execute, and a
    ``"prepares_only": true`` cannot make the client claim a message was sent.
    Both are re-applied by :meth:`_Action._apply_policy` afterwards.
    """
    if isinstance(value, dict):
        for key in _SERVER_POLICY_KEYS:
            value.pop(key, None)
    return value


class _StrictModel(BaseModel):
    """Base model: no unknown keys, no silent coercion of odd types."""

    model_config = ConfigDict(extra="forbid")


class _Action(_StrictModel):
    """Common shape of every whitelisted action.

    Attributes:
        type: Discriminator identifying the action.
        requires_confirmation: Computed server-side from
            :data:`REQUIRES_CONFIRMATION`; never taken from the model.
        prepares_only: Computed server-side from :data:`PREPARES_ONLY`;
            ``True`` for the actions that only draft something for the user
            to finish by hand.
    """

    type: str
    requires_confirmation: bool = False
    prepares_only: bool = False

    @model_validator(mode="before")
    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        return _strip_model_supplied_policy(value)

    @model_validator(mode="after")
    def _apply_policy(self) -> "_Action":
        """Recompute the policy fields from this action's type."""
        action_type = DeviceActionType(self.type)
        self.requires_confirmation = REQUIRES_CONFIRMATION[action_type]
        self.prepares_only = action_type in PREPARES_ONLY
        return self


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DeviceActionType(str, Enum):
    """The closed set of action types the model may emit."""

    SET_VOLUME = "SET_VOLUME"
    SET_BRIGHTNESS = "SET_BRIGHTNESS"
    TOGGLE_FLASHLIGHT = "TOGGLE_FLASHLIGHT"
    OPEN_APP = "OPEN_APP"
    OPEN_SETTINGS_PANEL = "OPEN_SETTINGS_PANEL"
    DIAL = "DIAL"
    COMPOSE_MESSAGE = "COMPOSE_MESSAGE"
    SET_ALARM = "SET_ALARM"
    SET_TIMER = "SET_TIMER"
    CREATE_CALENDAR_EVENT = "CREATE_CALENDAR_EVENT"
    NAVIGATE = "NAVIGATE"
    WEB_SEARCH = "WEB_SEARCH"
    PLAY_MUSIC = "PLAY_MUSIC"
    MARK_DAY_COMPLETED = "MARK_DAY_COMPLETED"
    WHAT_IS_PENDING_TODAY = "WHAT_IS_PENDING_TODAY"


class VolumeStream(str, Enum):
    """Which audio stream a volume change applies to."""

    MEDIA = "media"
    RING = "ring"
    ALARM = "alarm"


class SettingsPanel(str, Enum):
    """Which system settings panel to open."""

    WIFI = "wifi"
    BLUETOOTH = "bluetooth"
    INTERNET = "internet"
    VOLUME = "volume"
    DISPLAY = "display"


class MessagingApp(str, Enum):
    """Which messaging app a draft is prepared in.

    The message is **never sent by the backend** — see this module's
    docstring.
    """

    SMS = "sms"
    WHATSAPP = "whatsapp"


#: Actions whose execution must be preceded by an explicit user confirmation.
#: This is a server-side policy table: the model cannot influence it.
REQUIRES_CONFIRMATION: dict[DeviceActionType, bool] = {
    DeviceActionType.COMPOSE_MESSAGE: True,
    DeviceActionType.DIAL: True,
    DeviceActionType.MARK_DAY_COMPLETED: True,
    DeviceActionType.SET_VOLUME: False,
    DeviceActionType.SET_BRIGHTNESS: False,
    DeviceActionType.TOGGLE_FLASHLIGHT: False,
    DeviceActionType.OPEN_APP: False,
    DeviceActionType.OPEN_SETTINGS_PANEL: False,
    DeviceActionType.SET_ALARM: False,
    DeviceActionType.SET_TIMER: False,
    DeviceActionType.CREATE_CALENDAR_EVENT: False,
    DeviceActionType.NAVIGATE: False,
    DeviceActionType.WEB_SEARCH: False,
    DeviceActionType.PLAY_MUSIC: False,
    DeviceActionType.WHAT_IS_PENDING_TODAY: False,
}

#: Actions that only prepare a draft for the user to finish by hand.
#: The client must **not** report these as completed.
PREPARES_ONLY: frozenset[DeviceActionType] = frozenset(
    {DeviceActionType.DIAL, DeviceActionType.COMPOSE_MESSAGE}
)


# ---------------------------------------------------------------------------
# Parameter models
# ---------------------------------------------------------------------------


class NoParams(_StrictModel):
    """Empty parameter set, for actions that take no arguments."""


class SetVolumeParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.SET_VOLUME`."""

    stream: VolumeStream
    percent: int = Field(ge=0, le=100, strict=True)


class SetBrightnessParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.SET_BRIGHTNESS`."""

    percent: int = Field(ge=0, le=100, strict=True)


class ToggleFlashlightParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.TOGGLE_FLASHLIGHT`."""

    on: bool = Field(strict=True)


class OpenAppParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.OPEN_APP`."""

    app_name: str = Field(min_length=1, max_length=120)


class OpenSettingsPanelParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.OPEN_SETTINGS_PANEL`."""

    panel: SettingsPanel


class DialParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.DIAL`."""


class ComposeMessageParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.COMPOSE_MESSAGE`."""

    app: MessagingApp
    contact_name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=1000)


class SetAlarmParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.SET_ALARM`."""

    hour: int = Field(ge=0, le=23, strict=True)
    minute: int = Field(ge=0, le=59, strict=True)
    label: str | None = Field(default=None, max_length=120)


class SetTimerParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.SET_TIMER`."""

    seconds: int = Field(ge=1, le=86_400, strict=True)
    label: str | None = Field(default=None, max_length=120)


class CreateCalendarEventParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.CREATE_CALENDAR_EVENT`.

    ``start_iso`` must be an ISO-8601 timestamp.  A value without an offset
    is read as UTC, matching how the rest of the app stores timestamps.
    ``end_iso``, when given, must be strictly after ``start_iso``.
    """

    title: str = Field(min_length=1, max_length=200)
    start_iso: str = Field(min_length=1, max_length=64)
    end_iso: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _check_window(self) -> "CreateCalendarEventParams":
        start = self._parse(self.start_iso, "start_iso")
        if self.end_iso is not None:
            end = self._parse(self.end_iso, "end_iso")
            if end <= start:
                raise ValueError("end_iso must be after start_iso")
        return self

    @staticmethod
    def _parse(value: str, field: str) -> datetime:
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc


class NavigateParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.NAVIGATE`."""

    destination: str = Field(min_length=1, max_length=200)


class WebSearchParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.WEB_SEARCH`."""

    query: str = Field(min_length=1, max_length=300)


class PlayMusicParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.PLAY_MUSIC`."""

    query: str = Field(min_length=1, max_length=300)


class MarkDayCompletedParams(_StrictModel):
    """Parameters for :attr:`DeviceActionType.MARK_DAY_COMPLETED`.

    The ids must come from the data block the server supplies; the service
    re-checks ownership of both before returning the action, so a model
    cannot point at another user's roadmap.
    """

    objective_id: int = Field(ge=1, strict=True)
    day_id: int = Field(ge=1, strict=True)


# ---------------------------------------------------------------------------
# Action models
# ---------------------------------------------------------------------------


class SetVolumeAction(_Action):
    """Set the volume of one audio stream to a percentage."""

    type: Literal[DeviceActionType.SET_VOLUME] = DeviceActionType.SET_VOLUME
    params: SetVolumeParams


class SetBrightnessAction(_Action):
    """Set the screen brightness to a percentage."""

    type: Literal[DeviceActionType.SET_BRIGHTNESS] = DeviceActionType.SET_BRIGHTNESS
    params: SetBrightnessParams


class ToggleFlashlightAction(_Action):
    """Turn the flashlight on or off."""

    type: Literal[DeviceActionType.TOGGLE_FLASHLIGHT] = DeviceActionType.TOGGLE_FLASHLIGHT
    params: ToggleFlashlightParams


class OpenAppAction(_Action):
    """Open an application by name."""

    type: Literal[DeviceActionType.OPEN_APP] = DeviceActionType.OPEN_APP
    params: OpenAppParams


class OpenSettingsPanelAction(_Action):
    """Open a system settings panel."""

    type: Literal[DeviceActionType.OPEN_SETTINGS_PANEL] = DeviceActionType.OPEN_SETTINGS_PANEL
    params: OpenSettingsPanelParams


class DialAction(_Action):
    """Open the dialer with a contact ready to call.

    **Prepares only.**  The user still presses the call button; this action
    never places a call.
    """

    type: Literal[DeviceActionType.DIAL] = DeviceActionType.DIAL
    params: DialParams
    # Declared to match the policy tables so the generated OpenAPI schema does
    # not advertise a permissive default for a confirmed action.
    requires_confirmation: bool = REQUIRES_CONFIRMATION[DeviceActionType.DIAL]
    prepares_only: bool = True


class ComposeMessageAction(_Action):
    """Open a messaging app with a draft addressed to a contact.

    **Prepares only.**  The user still presses send; this action never
    sends a message.
    """

    type: Literal[DeviceActionType.COMPOSE_MESSAGE] = DeviceActionType.COMPOSE_MESSAGE
    params: ComposeMessageParams
    requires_confirmation: bool = REQUIRES_CONFIRMATION[DeviceActionType.COMPOSE_MESSAGE]
    prepares_only: bool = True


class SetAlarmAction(_Action):
    """Create an alarm."""

    type: Literal[DeviceActionType.SET_ALARM] = DeviceActionType.SET_ALARM
    params: SetAlarmParams


class SetTimerAction(_Action):
    """Start a countdown timer."""

    type: Literal[DeviceActionType.SET_TIMER] = DeviceActionType.SET_TIMER
    params: SetTimerParams


class CreateCalendarEventAction(_Action):
    """Create a calendar event."""

    type: Literal[DeviceActionType.CREATE_CALENDAR_EVENT] = DeviceActionType.CREATE_CALENDAR_EVENT
    params: CreateCalendarEventParams


class NavigateAction(_Action):
    """Start turn-by-turn navigation to a destination."""

    type: Literal[DeviceActionType.NAVIGATE] = DeviceActionType.NAVIGATE
    params: NavigateParams


class WebSearchAction(_Action):
    """Open a web search for a query."""

    type: Literal[DeviceActionType.WEB_SEARCH] = DeviceActionType.WEB_SEARCH
    params: WebSearchParams


class PlayMusicAction(_Action):
    """Play music matching a query."""

    type: Literal[DeviceActionType.PLAY_MUSIC] = DeviceActionType.PLAY_MUSIC
    params: PlayMusicParams


class MarkDayCompletedAction(_Action):
    """Mark a roadmap day as completed.

    The server verifies that both ids belong to the caller before returning
    this action.  The day is **not** marked here: the client confirms with
    the user and then calls
    ``PATCH /api/v1/objectives/{objective_id}/roadmap/days/{day_id}``, which
    performs the owner-checked write.  Marking it inside this request would
    make ``requires_confirmation`` meaningless.
    """

    type: Literal[DeviceActionType.MARK_DAY_COMPLETED] = DeviceActionType.MARK_DAY_COMPLETED
    params: MarkDayCompletedParams
    requires_confirmation: bool = REQUIRES_CONFIRMATION[DeviceActionType.MARK_DAY_COMPLETED]


class WhatIsPendingTodayAction(_Action):
    """Answer with the roadmap items pending today.

    Resolved **server-side**: the service renders the facts from the
    database and the model's prose is discarded, so the answer cannot be
    hallucinated.
    """

    type: Literal[DeviceActionType.WHAT_IS_PENDING_TODAY] = DeviceActionType.WHAT_IS_PENDING_TODAY
    params: NoParams


DeviceAction = Annotated[
    SetVolumeAction
    | SetBrightnessAction
    | ToggleFlashlightAction
    | OpenAppAction
    | OpenSettingsPanelAction
    | DialAction
    | ComposeMessageAction
    | SetAlarmAction
    | SetTimerAction
    | CreateCalendarEventAction
    | NavigateAction
    | WebSearchAction
    | PlayMusicAction
    | MarkDayCompletedAction
    | WhatIsPendingTodayAction,
    Field(discriminator="type"),
]

#: Validates a raw ``action`` object from the model against the whitelist.
#: A :class:`pydantic.ValidationError` means "not whitelisted".
DeviceActionAdapter: TypeAdapter[DeviceAction] = TypeAdapter(DeviceAction)
