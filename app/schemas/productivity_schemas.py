"""Pydantic schemas for the personal-productivity endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.productivity import (
    ChecklistCategory,
    Recurrence,
    ReminderStatus,
    TaskPriority,
    TaskStatus,
)


class PageMeta(BaseModel):
    """Pagination metadata shared by every list endpoint."""

    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    """Request body for creating a task."""

    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    recurrence: Recurrence = Recurrence.NONE


class TaskUpdate(BaseModel):
    """Request body for patching a task (all fields optional)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    due_at: datetime | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    recurrence: Recurrence | None = None


class TaskResponse(BaseModel):
    """Response body for a single task."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    notes: str | None = None
    due_at: datetime | None = None
    priority: TaskPriority
    status: TaskStatus
    recurrence: Recurrence
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Paginated task list."""

    items: list[TaskResponse]
    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
class ReminderCreate(BaseModel):
    """Request body for creating a reminder."""

    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=5000)
    remind_at: datetime
    recurrence: Recurrence = Recurrence.NONE


class ReminderUpdate(BaseModel):
    """Request body for patching a reminder."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=5000)
    remind_at: datetime | None = None
    recurrence: Recurrence | None = None
    status: ReminderStatus | None = None


class ReminderSnooze(BaseModel):
    """Request body for snoozing a reminder."""

    snoozed_until: datetime


class ReminderResponse(BaseModel):
    """Response body for a single reminder."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    body: str | None = None
    remind_at: datetime
    recurrence: Recurrence
    status: ReminderStatus
    snoozed_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    """Paginated reminder list."""

    items: list[ReminderResponse]
    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
class NoteCreate(BaseModel):
    """Request body for creating a note."""

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=50_000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    pinned: bool = False


class NoteUpdate(BaseModel):
    """Request body for patching a note."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, max_length=50_000)
    tags: list[str] | None = Field(default=None, max_length=30)
    pinned: bool | None = None


class NoteResponse(BaseModel):
    """Response body for a single note."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    tags: list[str]
    pinned: bool
    created_at: datetime
    updated_at: datetime


class NoteListResponse(BaseModel):
    """Paginated note list."""

    items: list[NoteResponse]
    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------
class ChecklistItemCreate(BaseModel):
    """Request body for adding an item to a checklist."""

    text: str = Field(min_length=1, max_length=300)
    position: int = Field(default=0, ge=0)


class ChecklistItemUpdate(BaseModel):
    """Request body for patching a checklist item."""

    text: str | None = Field(default=None, min_length=1, max_length=300)
    done: bool | None = None
    position: int | None = Field(default=None, ge=0)


class ChecklistItemResponse(BaseModel):
    """Response body for a single checklist item."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    checklist_id: int
    text: str
    done: bool
    position: int
    created_at: datetime


class ChecklistCreate(BaseModel):
    """Request body for creating a checklist."""

    title: str = Field(min_length=1, max_length=200)
    category: ChecklistCategory = ChecklistCategory.COMPRAS


class ChecklistUpdate(BaseModel):
    """Request body for patching a checklist."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: ChecklistCategory | None = None


class ChecklistResponse(BaseModel):
    """Response body for a checklist with its items."""

    id: int
    title: str
    category: ChecklistCategory
    items: list[ChecklistItemResponse]
    created_at: datetime
    updated_at: datetime


class ChecklistListResponse(BaseModel):
    """Paginated checklist list."""

    items: list[ChecklistResponse]
    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------
class HabitCreate(BaseModel):
    """Request body for creating a habit."""

    title: str = Field(min_length=1, max_length=200)


class HabitResponse(BaseModel):
    """Response body for a habit with its streak and recent check-ins."""

    id: int
    title: str
    streak: int
    check_ins: list[date]
    created_at: datetime


class HabitListResponse(BaseModel):
    """Paginated habit list."""

    items: list[HabitResponse]
    page: int
    limit: int
    total: int


class HabitCheckInRequest(BaseModel):
    """Optional body for a habit check-in (defaults to the user's today)."""

    check_date: date | None = None


# ---------------------------------------------------------------------------
# Focus sessions
# ---------------------------------------------------------------------------
class FocusSessionCreate(BaseModel):
    """Request body for starting a focus session."""

    label: str = Field(default="Foco", min_length=1, max_length=200)
    start_at: datetime
    end_at: datetime | None = None


class FocusSessionUpdate(BaseModel):
    """Request body for patching a focus session (e.g. to finish it)."""

    label: str | None = Field(default=None, min_length=1, max_length=200)
    end_at: datetime | None = None


class FocusSessionResponse(BaseModel):
    """Response body for a single focus session."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    start_at: datetime
    end_at: datetime | None = None
    created_at: datetime


class FocusSessionListResponse(BaseModel):
    """Paginated focus-session list."""

    items: list[FocusSessionResponse]
    page: int
    limit: int
    total: int


# ---------------------------------------------------------------------------
# Agenda / today
# ---------------------------------------------------------------------------
class AgendaItem(BaseModel):
    """One merged agenda entry (task, reminder or roadmap day)."""

    kind: str
    id: int
    title: str
    at: datetime
    status: str
    meta: dict[str, object] = Field(default_factory=dict)


class AgendaResponse(BaseModel):
    """Chronologically merged agenda for a window."""

    from_: datetime
    to: datetime
    timezone: str
    items: list[AgendaItem]


class TodayResponse(BaseModel):
    """A summary of the user's local day."""

    date: date
    timezone: str
    tasks: list[AgendaItem]
    reminders: list[AgendaItem]
    roadmap_days: list[AgendaItem]
    habits: list[HabitResponse]
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# Weekly plan
# ---------------------------------------------------------------------------
class WeeklyPlanRequest(BaseModel):
    """Request body for generating a weekly plan."""

    focus: str | None = Field(default=None, max_length=500)


class WeeklyPlanSuggestion(BaseModel):
    """One structured suggestion returned by the AI (not yet persisted)."""

    kind: str
    title: str
    due_at: datetime | None = None
    remind_at: datetime | None = None
    priority: TaskPriority = TaskPriority.MEDIUM
    notes: str | None = None


class WeeklyPlanResponse(BaseModel):
    """The cached (not yet persisted) weekly-plan proposal."""

    suggestions: list[WeeklyPlanSuggestion]
    disclaimer: str


class WeeklyPlanAcceptResponse(BaseModel):
    """Result of accepting a weekly plan."""

    created_tasks: int
    created_reminders: int
