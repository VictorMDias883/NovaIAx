"""
Strict schema whitelist for the actions the orchestrating agent may propose.

The model is an untrusted *parser*, never an executor.  Its raw JSON is
validated against :data:`AgentActionAdapter`; a type, a parameter name, a
range or an extra key that is not declared below is **rejected** and the
action is dropped.  There is no permissive fallback: "close enough" is the
same as "not whitelisted".

Two families live here:

* **backend actions** (``CREATE_TASK`` … ``MARK_DAY_COMPLETED``) — parameters
  validated here and executed by :mod:`app.services.agent_orchestrator_service`
  only after the user confirms;
* **device actions** — parameters reuse the existing strict models from
  :mod:`app.schemas.device_action_schemas` and are returned to the app to run
  locally.

Policy fields (``requires_confirmation``, ``prepares_only``) are **not**
declared on the models: they are recomputed server-side from
:mod:`app.models.agent_action`, so a prompt-injected ``"requires_confirmation":
false`` cannot downgrade an action.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models.agent_action import AgentActionType
from app.models.productivity import Recurrence, TaskPriority
from app.schemas.device_action_schemas import (
    ComposeMessageParams,
    CreateCalendarEventParams,
    DialParams,
    MarkDayCompletedParams,
    NavigateParams,
    NoParams,
    OpenAppParams,
    OpenSettingsPanelParams,
    PlayMusicParams,
    SetAlarmParams,
    SetBrightnessParams,
    SetTimerParams,
    SetVolumeParams,
    ToggleFlashlightParams,
    WebSearchParams,
)


class _StrictModel(BaseModel):
    """Base model: no unknown keys, no silent coercion of odd types."""

    model_config = ConfigDict(extra="forbid")


class _AgentAction(_StrictModel):
    """Common shape of every whitelisted agent action."""

    type: str


# ---------------------------------------------------------------------------
# Backend-action parameter models
# ---------------------------------------------------------------------------


class CreateTaskParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.CREATE_TASK`."""

    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    priority: TaskPriority | None = None
    recurrence: Recurrence | None = None


class CreateReminderParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.CREATE_REMINDER`."""

    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=5000)
    remind_at: datetime
    recurrence: Recurrence | None = None


class CreateNoteParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.CREATE_NOTE`."""

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=50_000)
    tags: list[str] = Field(default_factory=list, max_length=30)


class AddChecklistItemParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.ADD_CHECKLIST_ITEM`."""

    checklist_id: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=300)


class LogExpenseParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.LOG_EXPENSE`."""

    amount: float = Field(gt=0)
    category: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = None


class LogMedicationTakenParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.LOG_MEDICATION_TAKEN`."""

    medication_id: int = Field(ge=1, strict=True)
    taken_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=500)


class StartStudyPlanParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.START_STUDY_PLAN`."""

    subject: str = Field(min_length=1, max_length=200)
    focus: str | None = Field(default=None, max_length=500)


class FlashcardItem(_StrictModel):
    """A single front/back flashcard."""

    front: str = Field(min_length=1, max_length=500)
    back: str = Field(min_length=1, max_length=500)


class CreateFlashcardsParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.CREATE_FLASHCARDS`."""

    deck: str | None = Field(default=None, max_length=200)
    cards: list[FlashcardItem] = Field(min_length=1, max_length=100)


class ShowAgendaParams(_StrictModel):
    """Parameters for :attr:`AgentActionType.SHOW_AGENDA`."""

    date: _date | None = None


# ---------------------------------------------------------------------------
# Backend action models (server-executed)
# ---------------------------------------------------------------------------


class CreateTaskAction(_AgentAction):
    type: Literal[AgentActionType.CREATE_TASK] = AgentActionType.CREATE_TASK
    params: CreateTaskParams


class CreateReminderAction(_AgentAction):
    type: Literal[AgentActionType.CREATE_REMINDER] = AgentActionType.CREATE_REMINDER
    params: CreateReminderParams


class CreateNoteAction(_AgentAction):
    type: Literal[AgentActionType.CREATE_NOTE] = AgentActionType.CREATE_NOTE
    params: CreateNoteParams


class AddChecklistItemAction(_AgentAction):
    type: Literal[AgentActionType.ADD_CHECKLIST_ITEM] = AgentActionType.ADD_CHECKLIST_ITEM
    params: AddChecklistItemParams


class LogExpenseAction(_AgentAction):
    type: Literal[AgentActionType.LOG_EXPENSE] = AgentActionType.LOG_EXPENSE
    params: LogExpenseParams


class LogMedicationTakenAction(_AgentAction):
    type: Literal[AgentActionType.LOG_MEDICATION_TAKEN] = AgentActionType.LOG_MEDICATION_TAKEN
    params: LogMedicationTakenParams


class StartStudyPlanAction(_AgentAction):
    type: Literal[AgentActionType.START_STUDY_PLAN] = AgentActionType.START_STUDY_PLAN
    params: StartStudyPlanParams


class CreateFlashcardsAction(_AgentAction):
    type: Literal[AgentActionType.CREATE_FLASHCARDS] = AgentActionType.CREATE_FLASHCARDS
    params: CreateFlashcardsParams


class ShowAgendaAction(_AgentAction):
    type: Literal[AgentActionType.SHOW_AGENDA] = AgentActionType.SHOW_AGENDA
    params: ShowAgendaParams


class MarkDayCompletedAction(_AgentAction):
    """Mark a roadmap day completed (owner-checked at execution)."""

    type: Literal[AgentActionType.MARK_DAY_COMPLETED] = AgentActionType.MARK_DAY_COMPLETED
    params: MarkDayCompletedParams


# ---------------------------------------------------------------------------
# Device action models (app-executed)
# ---------------------------------------------------------------------------


class SetVolumeAction(_AgentAction):
    type: Literal[AgentActionType.SET_VOLUME] = AgentActionType.SET_VOLUME
    params: SetVolumeParams


class SetBrightnessAction(_AgentAction):
    type: Literal[AgentActionType.SET_BRIGHTNESS] = AgentActionType.SET_BRIGHTNESS
    params: SetBrightnessParams


class ToggleFlashlightAction(_AgentAction):
    type: Literal[AgentActionType.TOGGLE_FLASHLIGHT] = AgentActionType.TOGGLE_FLASHLIGHT
    params: ToggleFlashlightParams


class OpenAppAction(_AgentAction):
    type: Literal[AgentActionType.OPEN_APP] = AgentActionType.OPEN_APP
    params: OpenAppParams


class OpenSettingsPanelAction(_AgentAction):
    type: Literal[AgentActionType.OPEN_SETTINGS_PANEL] = AgentActionType.OPEN_SETTINGS_PANEL
    params: OpenSettingsPanelParams


class DialAction(_AgentAction):
    type: Literal[AgentActionType.DIAL] = AgentActionType.DIAL
    params: DialParams


class ComposeMessageAction(_AgentAction):
    type: Literal[AgentActionType.COMPOSE_MESSAGE] = AgentActionType.COMPOSE_MESSAGE
    params: ComposeMessageParams


class SetAlarmAction(_AgentAction):
    type: Literal[AgentActionType.SET_ALARM] = AgentActionType.SET_ALARM
    params: SetAlarmParams


class SetTimerAction(_AgentAction):
    type: Literal[AgentActionType.SET_TIMER] = AgentActionType.SET_TIMER
    params: SetTimerParams


class CreateCalendarEventAction(_AgentAction):
    type: Literal[AgentActionType.CREATE_CALENDAR_EVENT] = AgentActionType.CREATE_CALENDAR_EVENT
    params: CreateCalendarEventParams


class NavigateAction(_AgentAction):
    type: Literal[AgentActionType.NAVIGATE] = AgentActionType.NAVIGATE
    params: NavigateParams


class WebSearchAction(_AgentAction):
    type: Literal[AgentActionType.WEB_SEARCH] = AgentActionType.WEB_SEARCH
    params: WebSearchParams


class PlayMusicAction(_AgentAction):
    type: Literal[AgentActionType.PLAY_MUSIC] = AgentActionType.PLAY_MUSIC
    params: PlayMusicParams


class WhatIsPendingTodayAction(_AgentAction):
    type: Literal[AgentActionType.WHAT_IS_PENDING_TODAY] = AgentActionType.WHAT_IS_PENDING_TODAY
    params: NoParams


AgentAction = Annotated[
    CreateTaskAction
    | CreateReminderAction
    | CreateNoteAction
    | AddChecklistItemAction
    | LogExpenseAction
    | LogMedicationTakenAction
    | StartStudyPlanAction
    | CreateFlashcardsAction
    | ShowAgendaAction
    | MarkDayCompletedAction
    | SetVolumeAction
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
    | WhatIsPendingTodayAction,
    Field(discriminator="type"),
]

#: Validates a raw ``action`` object from the model against the whitelist.
#: A :class:`pydantic.ValidationError` means "not whitelisted".
AgentActionAdapter: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)
