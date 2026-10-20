"""
ORM models for the agent tool-orchestration audit trail.

The agent chat endpoint lets the model *propose* actions, but nothing the
model emits is ever executed straight away.  Every proposal is validated
against a strict Pydantic whitelist and recorded here as a row in
:class:`AgentAction`; a backend action only runs after the user explicitly
confirms it, and device actions are handed back to the app to execute locally.

The lifecycle encoded by :class:`AgentActionStatus` is the audit story::

    PROPOSED ──confirm──▶ CONFIRMED ──run──▶ EXECUTED
        │                     │
        │                     ├──run fails──▶ FAILED
        │                     └──user cancels──▶ CANCELED
        └──reject──▶ REJECTED

Device actions end at ``CONFIRMED`` (the server cannot observe the local
execution); the app reports the outcome back, moving the row to
``EXECUTED``/``FAILED``/``CANCELED``.

All datetimes are UTC.  Native enums use uppercase *member names* (the
SQLAlchemy storage convention used across the schema) with lower-case
*values* surfaced to the API.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONDict


class AgentActionType(str, Enum):
    """The closed set of tools the agent may propose.

    The first block is executed **by the backend** after confirmation (or
    resolved server-side, for ``SHOW_AGENDA``); the second block mirrors the
    existing device-command whitelist and is returned to the app for **local**
    execution.
    """

    # Backend actions.
    CREATE_TASK = "CREATE_TASK"
    CREATE_REMINDER = "CREATE_REMINDER"
    CREATE_NOTE = "CREATE_NOTE"
    ADD_CHECKLIST_ITEM = "ADD_CHECKLIST_ITEM"
    LOG_EXPENSE = "LOG_EXPENSE"
    LOG_MEDICATION_TAKEN = "LOG_MEDICATION_TAKEN"
    START_STUDY_PLAN = "START_STUDY_PLAN"
    CREATE_FLASHCARDS = "CREATE_FLASHCARDS"
    SHOW_AGENDA = "SHOW_AGENDA"
    MARK_DAY_COMPLETED = "MARK_DAY_COMPLETED"

    # Device actions (executed locally by the app).
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
    WHAT_IS_PENDING_TODAY = "WHAT_IS_PENDING_TODAY"


#: Action types the backend runs itself once the user confirms.
BACKEND_ACTION_TYPES: frozenset[AgentActionType] = frozenset(
    {
        AgentActionType.CREATE_TASK,
        AgentActionType.CREATE_REMINDER,
        AgentActionType.CREATE_NOTE,
        AgentActionType.ADD_CHECKLIST_ITEM,
        AgentActionType.LOG_EXPENSE,
        AgentActionType.LOG_MEDICATION_TAKEN,
        AgentActionType.START_STUDY_PLAN,
        AgentActionType.CREATE_FLASHCARDS,
        AgentActionType.SHOW_AGENDA,
        AgentActionType.MARK_DAY_COMPLETED,
    }
)

#: Action types executed by the mobile app, never by the server.
DEVICE_ACTION_TYPES: frozenset[AgentActionType] = frozenset(
    {
        AgentActionType.SET_VOLUME,
        AgentActionType.SET_BRIGHTNESS,
        AgentActionType.TOGGLE_FLASHLIGHT,
        AgentActionType.OPEN_APP,
        AgentActionType.OPEN_SETTINGS_PANEL,
        AgentActionType.DIAL,
        AgentActionType.COMPOSE_MESSAGE,
        AgentActionType.SET_ALARM,
        AgentActionType.SET_TIMER,
        AgentActionType.CREATE_CALENDAR_EVENT,
        AgentActionType.NAVIGATE,
        AgentActionType.WEB_SEARCH,
        AgentActionType.PLAY_MUSIC,
        AgentActionType.WHAT_IS_PENDING_TODAY,
    }
)

#: Device actions that only prepare a draft for the user to finish by hand.
PREPARES_ONLY_TYPES: frozenset[AgentActionType] = frozenset(
    {AgentActionType.DIAL, AgentActionType.COMPOSE_MESSAGE}
)

#: Device actions that must be preceded by an explicit user confirmation
#: (server-side policy; the model cannot influence it).
DEVICE_REQUIRE_CONFIRMATION: dict[AgentActionType, bool] = {
    AgentActionType.DIAL: True,
    AgentActionType.COMPOSE_MESSAGE: True,
    AgentActionType.WHAT_IS_PENDING_TODAY: False,
    AgentActionType.SET_VOLUME: False,
    AgentActionType.SET_BRIGHTNESS: False,
    AgentActionType.TOGGLE_FLASHLIGHT: False,
    AgentActionType.OPEN_APP: False,
    AgentActionType.OPEN_SETTINGS_PANEL: False,
    AgentActionType.SET_ALARM: False,
    AgentActionType.SET_TIMER: False,
    AgentActionType.CREATE_CALENDAR_EVENT: False,
    AgentActionType.NAVIGATE: False,
    AgentActionType.WEB_SEARCH: False,
    AgentActionType.PLAY_MUSIC: False,
}


class AgentActionStatus(str, Enum):
    """Audit lifecycle of one proposed action."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELED = "canceled"


class AgentAction(Base):
    """One validated action the agent proposed, with its audit state.

    Table: ``agent_actions``
    """

    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type: Mapped[AgentActionType] = mapped_column(
        SQLEnum(AgentActionType, name="agent_action_type"), index=True, nullable=False
    )
    status: Mapped[AgentActionStatus] = mapped_column(
        SQLEnum(AgentActionStatus, name="agent_action_status"),
        index=True,
        nullable=False,
        default=AgentActionStatus.PROPOSED,
    )
    #: Validated, whitelist-clean parameters.  Never the raw model output.
    params: Mapped[dict] = mapped_column(JSONDict, nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_device: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prepares_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Execution output (e.g. the created row id) or ``None`` until executed.
    result: Mapped[dict | None] = mapped_column(JSONDict, nullable=True)
    #: Short, non-sensitive failure reason when ``status`` is ``FAILED``.
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Stable machine-readable error code reported by the app (e.g.
    #: ``permission_denied``) when a device action failed or was canceled.
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: Optional human-readable detail supplied by the app alongside the result.
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
