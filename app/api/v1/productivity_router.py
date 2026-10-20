"""
API v1 personal-productivity endpoints.

All routes live under ``/me`` and operate only on the caller's own rows.
List endpoints are paginated (``page``/``limit``) and return a total.  The
agenda merges tasks, reminders and objective roadmap days chronologically;
``/me/today`` summarises the caller's current local day; ``/me/weekly-plan``
asks the AI for suggestions that are cached (never persisted) until the user
calls ``/me/weekly-plan/accept``.

The two weekly-plan endpoints are plan-gated behind the ``automation`` feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_redis_client, get_session, require_db_user, require_plan
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.clients.plan_limits import FEATURE_AUTOMATION
from app.models.productivity import (
    ChecklistCategory,
    ReminderStatus,
    TaskPriority,
    TaskStatus,
)
from app.schemas.productivity_schemas import (
    AgendaResponse,
    ChecklistCreate,
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
    ChecklistListResponse,
    ChecklistResponse,
    ChecklistUpdate,
    FocusSessionCreate,
    FocusSessionListResponse,
    FocusSessionResponse,
    FocusSessionUpdate,
    HabitCheckInRequest,
    HabitCreate,
    HabitListResponse,
    HabitResponse,
    NoteCreate,
    NoteListResponse,
    NoteResponse,
    NoteUpdate,
    ReminderCreate,
    ReminderListResponse,
    ReminderResponse,
    ReminderSnooze,
    ReminderUpdate,
    TaskCreate,
    TaskListResponse,
    TaskResponse,
    TaskUpdate,
    TodayResponse,
    WeeklyPlanAcceptResponse,
    WeeklyPlanRequest,
    WeeklyPlanResponse,
)
from app.services.productivity_service import ProductivityService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/me", tags=["productivity"])


def _checklist_response(checklist, items) -> ChecklistResponse:  # noqa: ANN001
    return ChecklistResponse(
        id=checklist.id,
        title=checklist.title,
        category=checklist.category,
        items=[ChecklistItemResponse.model_validate(item) for item in items],
        created_at=checklist.created_at,
        updated_at=checklist.updated_at,
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: TaskStatus | None = None,
    priority: TaskPriority | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> TaskListResponse:
    rows, total = await ProductivityService(session).list_tasks(
        current_user_id(current_user),
        page=page,
        limit=limit,
        status=status,
        priority=priority,
        due_from=due_from,
        due_to=due_to,
    )
    return TaskListResponse(
        items=[TaskResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(
    payload: TaskCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    task = await ProductivityService(session).create_task(current_user_id(current_user), payload)
    return TaskResponse.model_validate(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    task = await ProductivityService(session).get_task(current_user_id(current_user), task_id)
    return TaskResponse.model_validate(task)


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    payload: TaskUpdate,
    task_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    task = await ProductivityService(session).update_task(current_user_id(current_user), task_id, payload)
    return TaskResponse.model_validate(task)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_task(current_user_id(current_user), task_id)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
@router.get("/reminders", response_model=ReminderListResponse)
async def list_reminders(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: ReminderStatus | None = None,
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderListResponse:
    rows, total = await ProductivityService(session).list_reminders(
        current_user_id(current_user),
        page=page,
        limit=limit,
        status=status,
        from_at=from_at,
        to_at=to_at,
    )
    return ReminderListResponse(
        items=[ReminderResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/reminders", response_model=ReminderResponse, status_code=201)
async def create_reminder(
    payload: ReminderCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    reminder = await ProductivityService(session).create_reminder(current_user_id(current_user), payload)
    return ReminderResponse.model_validate(reminder)


@router.patch("/reminders/{reminder_id}", response_model=ReminderResponse)
async def update_reminder(
    payload: ReminderUpdate,
    reminder_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    reminder = await ProductivityService(session).update_reminder(current_user_id(current_user), reminder_id, payload)
    return ReminderResponse.model_validate(reminder)


@router.post("/reminders/{reminder_id}/snooze", response_model=ReminderResponse)
async def snooze_reminder(
    payload: ReminderSnooze,
    reminder_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderResponse:
    reminder = await ProductivityService(session).snooze_reminder(
        current_user_id(current_user), reminder_id, payload.snoozed_until
    )
    return ReminderResponse.model_validate(reminder)


@router.delete("/reminders/{reminder_id}", status_code=204)
async def delete_reminder(
    reminder_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_reminder(current_user_id(current_user), reminder_id)


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
@router.get("/notes", response_model=NoteListResponse)
async def list_notes(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    tag: str | None = None,
    pinned: bool | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> NoteListResponse:
    rows, total = await ProductivityService(session).list_notes(
        current_user_id(current_user), page=page, limit=limit, tag=tag, pinned=pinned
    )
    return NoteListResponse(
        items=[NoteResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/notes", response_model=NoteResponse, status_code=201)
async def create_note(
    payload: NoteCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> NoteResponse:
    note = await ProductivityService(session).create_note(current_user_id(current_user), payload)
    return NoteResponse.model_validate(note)


@router.get("/notes/{note_id}", response_model=NoteResponse)
async def get_note(
    note_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> NoteResponse:
    note = await ProductivityService(session).get_note(current_user_id(current_user), note_id)
    return NoteResponse.model_validate(note)


@router.patch("/notes/{note_id}", response_model=NoteResponse)
async def update_note(
    payload: NoteUpdate,
    note_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> NoteResponse:
    note = await ProductivityService(session).update_note(current_user_id(current_user), note_id, payload)
    return NoteResponse.model_validate(note)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_note(current_user_id(current_user), note_id)


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------
@router.get("/checklists", response_model=ChecklistListResponse)
async def list_checklists(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: ChecklistCategory | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistListResponse:
    service = ProductivityService(session)
    rows, total = await service.list_checklists(current_user_id(current_user), page=page, limit=limit, category=category)
    return ChecklistListResponse(
        items=[_checklist_response(row, row.items) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/checklists", response_model=ChecklistResponse, status_code=201)
async def create_checklist(
    payload: ChecklistCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistResponse:
    service = ProductivityService(session)
    checklist = await service.create_checklist(current_user_id(current_user), payload)
    return _checklist_response(checklist, [])


@router.get("/checklists/{checklist_id}", response_model=ChecklistResponse)
async def get_checklist(
    checklist_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistResponse:
    service = ProductivityService(session)
    checklist = await service.get_checklist(current_user_id(current_user), checklist_id)
    return _checklist_response(checklist, await service.list_checklist_items(current_user_id(current_user), checklist_id))


@router.patch("/checklists/{checklist_id}", response_model=ChecklistResponse)
async def update_checklist(
    payload: ChecklistUpdate,
    checklist_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistResponse:
    service = ProductivityService(session)
    checklist = await service.update_checklist(current_user_id(current_user), checklist_id, payload)
    return _checklist_response(checklist, await service.list_checklist_items(current_user_id(current_user), checklist_id))


@router.delete("/checklists/{checklist_id}", status_code=204)
async def delete_checklist(
    checklist_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_checklist(current_user_id(current_user), checklist_id)


@router.post("/checklists/{checklist_id}/items", response_model=ChecklistItemResponse, status_code=201)
async def add_checklist_item(
    payload: ChecklistItemCreate,
    checklist_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistItemResponse:
    item = await ProductivityService(session).add_checklist_item(current_user_id(current_user), checklist_id, payload)
    return ChecklistItemResponse.model_validate(item)


@router.patch("/checklists/{checklist_id}/items/{item_id}", response_model=ChecklistItemResponse)
async def update_checklist_item(
    payload: ChecklistItemUpdate,
    checklist_id: int,
    item_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ChecklistItemResponse:
    item = await ProductivityService(session).update_checklist_item(current_user_id(current_user), checklist_id, item_id, payload)
    return ChecklistItemResponse.model_validate(item)


@router.delete("/checklists/{checklist_id}/items/{item_id}", status_code=204)
async def delete_checklist_item(
    checklist_id: int,
    item_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_checklist_item(current_user_id(current_user), checklist_id, item_id)


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------
@router.get("/habits", response_model=HabitListResponse)
async def list_habits(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> HabitListResponse:
    items, total = await ProductivityService(session).list_habits(current_user_id(current_user), page=page, limit=limit)
    return HabitListResponse(items=[HabitResponse(**item) for item in items], page=page, limit=limit, total=total)


@router.post("/habits", response_model=HabitResponse, status_code=201)
async def create_habit(
    payload: HabitCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> HabitResponse:
    service = ProductivityService(session)
    habit = await service.create_habit(current_user_id(current_user), payload)
    return HabitResponse(**await service.get_habit(current_user_id(current_user), habit.id))


@router.get("/habits/{habit_id}", response_model=HabitResponse)
async def get_habit(
    habit_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> HabitResponse:
    data = await ProductivityService(session).get_habit(current_user_id(current_user), habit_id)
    return HabitResponse(**data)


@router.post("/habits/{habit_id}/check-ins", response_model=HabitResponse)
async def check_in_habit(
    habit_id: int,
    payload: HabitCheckInRequest | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> HabitResponse:
    data = await ProductivityService(session).check_in_habit(
        current_user_id(current_user), habit_id, payload.check_date if payload else None
    )
    return HabitResponse(**data)


@router.delete("/habits/{habit_id}", status_code=204)
async def delete_habit(
    habit_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_habit(current_user_id(current_user), habit_id)


# ---------------------------------------------------------------------------
# Focus sessions
# ---------------------------------------------------------------------------
@router.get("/focus-sessions", response_model=FocusSessionListResponse)
async def list_focus_sessions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FocusSessionListResponse:
    rows, total = await ProductivityService(session).list_focus_sessions(
        current_user_id(current_user), page=page, limit=limit, from_at=from_at, to_at=to_at
    )
    return FocusSessionListResponse(
        items=[FocusSessionResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/focus-sessions", response_model=FocusSessionResponse, status_code=201)
async def create_focus_session(
    payload: FocusSessionCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FocusSessionResponse:
    obj = await ProductivityService(session).create_focus_session(current_user_id(current_user), payload)
    return FocusSessionResponse.model_validate(obj)


@router.patch("/focus-sessions/{session_id}", response_model=FocusSessionResponse)
async def update_focus_session(
    payload: FocusSessionUpdate,
    session_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FocusSessionResponse:
    obj = await ProductivityService(session).update_focus_session(current_user_id(current_user), session_id, payload)
    return FocusSessionResponse.model_validate(obj)


@router.delete("/focus-sessions/{session_id}", status_code=204)
async def delete_focus_session(
    session_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await ProductivityService(session).delete_focus_session(current_user_id(current_user), session_id)


# ---------------------------------------------------------------------------
# Agenda / today
# ---------------------------------------------------------------------------
@router.get("/agenda", response_model=AgendaResponse)
async def get_agenda(
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> AgendaResponse:
    start = from_at or datetime.combine(datetime.now(UTC).date(), time.min)
    end = to_at or (start + timedelta(days=7))
    data = await ProductivityService(session).agenda(current_user_id(current_user), start, end)
    return AgendaResponse(**data)


@router.get("/today", response_model=TodayResponse)
async def get_today(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> TodayResponse:
    data = await ProductivityService(session).today(current_user_id(current_user))
    return TodayResponse(**data)


# ---------------------------------------------------------------------------
# Weekly plan (AI, plan-gated)
# ---------------------------------------------------------------------------
@router.post("/weekly-plan", response_model=WeeklyPlanResponse)
async def generate_weekly_plan(
    payload: WeeklyPlanRequest,
    entitlement: Entitlement = Depends(require_plan(FEATURE_AUTOMATION)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
) -> WeeklyPlanResponse:
    service = ProductivityService(session, redis_client=redis_client, ai_client=ai_router)
    data = await service.generate_weekly_plan(
        current_user_id(current_user),
        plan_tier=entitlement.plan,
        focus=payload.focus,
        redis_client=redis_client,
    )
    return WeeklyPlanResponse(**data)


@router.post("/weekly-plan/accept", response_model=WeeklyPlanAcceptResponse)
async def accept_weekly_plan(
    _: Entitlement = Depends(require_plan(FEATURE_AUTOMATION)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
) -> WeeklyPlanAcceptResponse:
    service = ProductivityService(session, redis_client=redis_client)
    data = await service.accept_weekly_plan(current_user_id(current_user), redis_client)
    return WeeklyPlanAcceptResponse(**data)
