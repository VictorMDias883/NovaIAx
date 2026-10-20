"""
Service layer for the personal-productivity domain.

The service owns the business rules the routers and the (future) agent-tool
layer share:

* owner-scoping — a row is only ever read or written for its own ``user_id``;
* UTC storage — incoming naive datetimes are interpreted in the user's
  ``/me/agent`` timezone and converted to UTC before persistence;
* recurrence — completing a recurring task/reminder rolls the next occurrence
  forward instead of leaving the series dead;
* habit streaks — computed on read from the check-in dates;
* the weekly-plan AI flow — suggestions are cached (never persisted) until the
  user explicitly accepts them.

    Router → Service → Repository → Database
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, AITaskType, PlanTier
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.productivity import (
    Recurrence,
    Reminder,
    ReminderStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from app.models.user_agent import UserAgent
from app.repositories.productivity_repository import (
    ChecklistRepository,
    FocusSessionRepository,
    HabitRepository,
    NoteRepository,
    ReminderRepository,
    RoadmapAgendaRepository,
    TaskRepository,
)
from app.utils.json_extraction import extract_json

logger = get_logger(__name__)

DEFAULT_TIMEZONE = "America/Sao_Paulo"

_WEEKLY_PLAN_CACHE_KEY = "productivity:weekly_plan:{user_id}"
_WEEKLY_PLAN_TTL_SECONDS = 3600

#: Educational/safety disclaimer surfaced alongside every AI-generated plan.
WEEKLY_PLAN_DISCLAIMER = (
    "Sugestões geradas por IA para ajudar na organização. Revise antes de aceitar: "
    "você é responsável pelas decisões e pela sua rotina."
)

_WEEKLY_PLAN_SYSTEM_PROMPT = (
    "Você ajuda a montar um plano semanal de produtividade. Responda EXCLUSIVAMENTE "
    "em JSON no formato "
    '{"suggestions": [{"kind": "task", "title": "string", "due_at": "YYYY-MM-DDTHH:MM:SS", '
    '"remind_at": null, "priority": "low|medium|high", "notes": "string ou null"}]}. '
    'Cada sugestão deve ter kind "task" (use due_at) ou "reminder" (use remind_at). '
    "Gere entre 3 e 10 itens curtos e realistas. Não inclua nenhum texto fora do JSON."
)


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO-8601 datetime from the model/cache, or return ``None``."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _advance(value: datetime, recurrence: Recurrence) -> datetime:
    """Return the next occurrence of a recurring ``value``."""
    if recurrence is Recurrence.DAILY:
        return value + timedelta(days=1)
    if recurrence is Recurrence.WEEKLY:
        return value + timedelta(weeks=1)
    if recurrence is Recurrence.MONTHLY:
        return value + timedelta(days=30)
    return value


class ProductivityService:
    """CRUD, agenda merging and weekly-plan orchestration for one user."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        redis_client=None,  # noqa: ANN001 - RedisClient, injected to avoid a hard import
        ai_client: AIRouter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.redis = redis_client
        self.ai_client = ai_client
        self.tasks = TaskRepository(session)
        self.reminders = ReminderRepository(session)
        self.notes = NoteRepository(session)
        self.checklists = ChecklistRepository(session)
        self.habits = HabitRepository(session)
        self.focus = FocusSessionRepository(session)
        self.roadmap = RoadmapAgendaRepository(session)

    # ------------------------------------------------------------------
    # Timezone helpers
    # ------------------------------------------------------------------
    async def _user_zone(self, user_id: int) -> ZoneInfo:
        """Return the user's IANA timezone (falling back to São Paulo)."""
        result = await self.session.execute(select(UserAgent.timezone).where(UserAgent.user_id == user_id))
        name = result.scalars().first() or DEFAULT_TIMEZONE
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TIMEZONE)

    @staticmethod
    def _as_utc(value: datetime, zone: ZoneInfo) -> datetime:
        """Interpret a naive datetime in ``zone`` and return it in UTC."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        return value.astimezone(UTC)

    @staticmethod
    def _local_day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
        """Return ``[start, end]`` of a local calendar day, expressed in UTC."""
        start = datetime.combine(day, time.min, tzinfo=zone)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        return start.astimezone(UTC), end.astimezone(UTC)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    async def create_task(self, user_id: int, payload) -> Task:  # noqa: ANN001 - Pydantic
        zone = await self._user_zone(user_id)
        due_at = self._as_utc(payload.due_at, zone) if payload.due_at else None
        return await self.tasks.create(
            user_id,
            title=payload.title,
            notes=payload.notes,
            due_at=due_at,
            priority=payload.priority,
            recurrence=payload.recurrence,
        )

    async def list_tasks(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        status=None,
        priority=None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
    ) -> tuple[list[Task], int]:
        zone = await self._user_zone(user_id)
        if due_from is not None:
            due_from = self._as_utc(due_from, zone)
        if due_to is not None:
            due_to = self._as_utc(due_to, zone)
        return await self.tasks.list_for_user(
            user_id,
            status=status,
            priority=priority,
            due_from=due_from,
            due_to=due_to,
            offset=(page - 1) * limit,
            limit=limit,
        )

    async def _owned_task(self, user_id: int, task_id: int) -> Task:
        task = await self.tasks.get(task_id)
        if task is None or task.user_id != user_id:
            raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        return task

    async def get_task(self, user_id: int, task_id: int) -> Task:
        return await self._owned_task(user_id, task_id)

    async def update_task(self, user_id: int, task_id: int, payload) -> Task:  # noqa: ANN001
        task = await self._owned_task(user_id, task_id)
        zone = await self._user_zone(user_id)
        fields = payload.model_dump(exclude_unset=True)
        if "due_at" in fields and fields["due_at"] is not None:
            fields["due_at"] = self._as_utc(fields["due_at"], zone)
        updated = await self.tasks.update(task, **fields)
        if (
            fields.get("status") is TaskStatus.DONE
            and updated.recurrence is not Recurrence.NONE
            and updated.due_at is not None
        ):
            await self.tasks.create(
                user_id,
                title=updated.title,
                notes=updated.notes,
                due_at=_advance(updated.due_at, updated.recurrence),
                priority=updated.priority,
                recurrence=updated.recurrence,
            )
        return updated

    async def delete_task(self, user_id: int, task_id: int) -> None:
        await self.tasks.delete(await self._owned_task(user_id, task_id))

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------
    async def create_reminder(self, user_id: int, payload) -> Reminder:  # noqa: ANN001
        zone = await self._user_zone(user_id)
        return await self.reminders.create(
            user_id,
            title=payload.title,
            body=payload.body,
            remind_at=self._as_utc(payload.remind_at, zone),
            recurrence=payload.recurrence,
        )

    async def list_reminders(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        status=None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> tuple[list[Reminder], int]:
        zone = await self._user_zone(user_id)
        if from_at is not None:
            from_at = self._as_utc(from_at, zone)
        if to_at is not None:
            to_at = self._as_utc(to_at, zone)
        return await self.reminders.list_for_user(
            user_id,
            status=status,
            from_at=from_at,
            to_at=to_at,
            offset=(page - 1) * limit,
            limit=limit,
        )

    async def _owned_reminder(self, user_id: int, reminder_id: int) -> Reminder:
        reminder = await self.reminders.get(reminder_id)
        if reminder is None or reminder.user_id != user_id:
            raise HTTPException(status_code=404, detail="Lembrete não encontrado")
        return reminder

    async def get_reminder(self, user_id: int, reminder_id: int) -> Reminder:
        return await self._owned_reminder(user_id, reminder_id)

    async def update_reminder(self, user_id: int, reminder_id: int, payload) -> Reminder:  # noqa: ANN001
        reminder = await self._owned_reminder(user_id, reminder_id)
        zone = await self._user_zone(user_id)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("remind_at") is not None:
            fields["remind_at"] = self._as_utc(fields["remind_at"], zone)
        updated = await self.reminders.update(reminder, **fields)
        if fields.get("status") is ReminderStatus.DONE and updated.recurrence is not Recurrence.NONE:
            await self.reminders.create(
                user_id,
                title=updated.title,
                body=updated.body,
                remind_at=_advance(updated.remind_at, updated.recurrence),
                recurrence=updated.recurrence,
            )
        return updated

    async def snooze_reminder(self, user_id: int, reminder_id: int, until: datetime) -> Reminder:
        reminder = await self._owned_reminder(user_id, reminder_id)
        zone = await self._user_zone(user_id)
        return await self.reminders.update(reminder, snoozed_until=self._as_utc(until, zone))

    async def delete_reminder(self, user_id: int, reminder_id: int) -> None:
        await self.reminders.delete(await self._owned_reminder(user_id, reminder_id))

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------
    async def create_note(self, user_id: int, payload):
        return await self.notes.create(
            user_id,
            title=payload.title,
            content=payload.content,
            tags=list(payload.tags),
            pinned=payload.pinned,
        )

    async def list_notes(self, user_id: int, *, page: int, limit: int, tag=None, pinned=None):
        return await self.notes.list_for_user(user_id, tag=tag, pinned=pinned, offset=(page - 1) * limit, limit=limit)

    async def _owned_note(self, user_id: int, note_id: int):
        note = await self.notes.get(note_id)
        if note is None or note.user_id != user_id:
            raise HTTPException(status_code=404, detail="Nota não encontrada")
        return note

    async def get_note(self, user_id: int, note_id: int):
        return await self._owned_note(user_id, note_id)

    async def update_note(self, user_id: int, note_id: int, payload):
        note = await self._owned_note(user_id, note_id)
        return await self.notes.update(note, **payload.model_dump(exclude_unset=True))

    async def delete_note(self, user_id: int, note_id: int) -> None:
        await self.notes.delete(await self._owned_note(user_id, note_id))

    # ------------------------------------------------------------------
    # Checklists
    # ------------------------------------------------------------------
    async def create_checklist(self, user_id: int, payload):
        return await self.checklists.create(user_id, title=payload.title, category=payload.category)

    async def list_checklists(self, user_id: int, *, page: int, limit: int, category=None):
        return await self.checklists.list_for_user(user_id, category=category, offset=(page - 1) * limit, limit=limit)

    async def _owned_checklist(self, user_id: int, checklist_id: int):
        checklist = await self.checklists.get(checklist_id)
        if checklist is None or checklist.user_id != user_id:
            raise HTTPException(status_code=404, detail="Checklist não encontrado")
        return checklist

    async def get_checklist(self, user_id: int, checklist_id: int):
        return await self._owned_checklist(user_id, checklist_id)

    async def update_checklist(self, user_id: int, checklist_id: int, payload):
        checklist = await self._owned_checklist(user_id, checklist_id)
        return await self.checklists.update(checklist, **payload.model_dump(exclude_unset=True))

    async def list_checklist_items(self, user_id: int, checklist_id: int):
        await self._owned_checklist(user_id, checklist_id)
        return await self.checklists.list_items(checklist_id)

    async def delete_checklist(self, user_id: int, checklist_id: int) -> None:
        await self.checklists.delete(await self._owned_checklist(user_id, checklist_id))

    async def add_checklist_item(self, user_id: int, checklist_id: int, payload):
        await self._owned_checklist(user_id, checklist_id)
        return await self.checklists.add_item(checklist_id, text=payload.text, position=payload.position)

    async def update_checklist_item(self, user_id: int, checklist_id: int, item_id: int, payload):
        await self._owned_checklist(user_id, checklist_id)
        item = await self.checklists.get_item(item_id)
        if item is None or item.checklist_id != checklist_id:
            raise HTTPException(status_code=404, detail="Item não encontrado")
        return await self.checklists.update_item(item, **payload.model_dump(exclude_unset=True))

    async def delete_checklist_item(self, user_id: int, checklist_id: int, item_id: int) -> None:
        await self._owned_checklist(user_id, checklist_id)
        item = await self.checklists.get_item(item_id)
        if item is None or item.checklist_id != checklist_id:
            raise HTTPException(status_code=404, detail="Item não encontrado")
        await self.checklists.delete_item(item)

    # ------------------------------------------------------------------
    # Habits
    # ------------------------------------------------------------------
    @staticmethod
    def _streak(dates_desc: list[date], today: date) -> int:
        """Return the current consecutive-day streak from check-in dates."""
        if not dates_desc:
            return 0
        present = set(dates_desc)
        cursor = today if today in present else today - timedelta(days=1)
        if cursor not in present:
            return 0
        streak = 0
        while cursor in present:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    async def _habit_view(self, habit, today: date) -> dict:  # noqa: ANN001
        dates = await self.habits.list_check_dates(habit.id)
        return {
            "id": habit.id,
            "title": habit.title,
            "streak": self._streak(dates, today),
            "check_ins": sorted(dates),
            "created_at": habit.created_at,
        }

    async def create_habit(self, user_id: int, payload):
        return await self.habits.create(user_id, title=payload.title)

    async def list_habits(self, user_id: int, *, page: int, limit: int):
        rows, total = await self.habits.list_for_user(user_id, offset=(page - 1) * limit, limit=limit)
        today = datetime.now(await self._user_zone(user_id)).date()
        items = [await self._habit_view(habit, today) for habit in rows]
        return items, total

    async def get_habit(self, user_id: int, habit_id: int) -> dict:
        habit = await self.habits.get(habit_id)
        if habit is None or habit.user_id != user_id:
            raise HTTPException(status_code=404, detail="Hábito não encontrado")
        today = datetime.now(await self._user_zone(user_id)).date()
        return await self._habit_view(habit, today)

    async def delete_habit(self, user_id: int, habit_id: int) -> None:
        habit = await self.habits.get(habit_id)
        if habit is None or habit.user_id != user_id:
            raise HTTPException(status_code=404, detail="Hábito não encontrado")
        await self.habits.delete(habit)

    async def check_in_habit(self, user_id: int, habit_id: int, check_date: date | None) -> dict:
        habit = await self.habits.get(habit_id)
        if habit is None or habit.user_id != user_id:
            raise HTTPException(status_code=404, detail="Hábito não encontrado")
        zone = await self._user_zone(user_id)
        target = check_date or datetime.now(zone).date()
        existing = await self.habits.get_check(habit_id, target)
        if existing is None:
            await self.habits.add_check(habit_id, target)
        return await self._habit_view(habit, datetime.now(zone).date())

    # ------------------------------------------------------------------
    # Focus sessions
    # ------------------------------------------------------------------
    async def create_focus_session(self, user_id: int, payload):
        zone = await self._user_zone(user_id)
        end_at = self._as_utc(payload.end_at, zone) if payload.end_at else None
        return await self.focus.create(
            user_id,
            label=payload.label,
            start_at=self._as_utc(payload.start_at, zone),
            end_at=end_at,
        )

    async def list_focus_sessions(self, user_id: int, *, page: int, limit: int, from_at=None, to_at=None):
        zone = await self._user_zone(user_id)
        if from_at is not None:
            from_at = self._as_utc(from_at, zone)
        if to_at is not None:
            to_at = self._as_utc(to_at, zone)
        return await self.focus.list_for_user(
            user_id, from_at=from_at, to_at=to_at, offset=(page - 1) * limit, limit=limit
        )

    async def _owned_focus(self, user_id: int, session_id: int):
        obj = await self.focus.get(session_id)
        if obj is None or obj.user_id != user_id:
            raise HTTPException(status_code=404, detail="Sessão de foco não encontrada")
        return obj

    async def update_focus_session(self, user_id: int, session_id: int, payload):
        obj = await self._owned_focus(user_id, session_id)
        zone = await self._user_zone(user_id)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("end_at") is not None:
            fields["end_at"] = self._as_utc(fields["end_at"], zone)
        return await self.focus.update(obj, **fields)

    async def delete_focus_session(self, user_id: int, session_id: int) -> None:
        await self.focus.delete(await self._owned_focus(user_id, session_id))

    # ------------------------------------------------------------------
    # Agenda / today
    # ------------------------------------------------------------------
    async def agenda(self, user_id: int, start: datetime, end: datetime) -> dict:
        """Merge tasks, reminders and roadmap days chronologically."""
        zone = await self._user_zone(user_id)
        start_utc = self._as_utc(start, zone)
        end_utc = self._as_utc(end, zone)

        tasks = await self.tasks.list_due_between(user_id, start_utc, end_utc)
        reminders = await self.reminders.list_between(user_id, start_utc, end_utc)
        days = await self.roadmap.list_days_between(user_id, start_utc, end_utc)

        items = (
            [self._task_item(t) for t in tasks]
            + [self._reminder_item(r) for r in reminders]
            + [self._roadmap_item(d) for d in days]
        )
        items.sort(key=lambda item: item["at"])
        return {
            "from_": start_utc,
            "to": end_utc,
            "timezone": str(zone),
            "items": items,
        }

    @staticmethod
    def _task_item(task: Task) -> dict:
        return {
            "kind": "task",
            "id": task.id,
            "title": task.title,
            "at": task.due_at or task.created_at,
            "status": task.status.value,
            "meta": {"priority": task.priority.value, "recurrence": task.recurrence.value},
        }

    @staticmethod
    def _reminder_item(reminder: Reminder) -> dict:
        return {
            "kind": "reminder",
            "id": reminder.id,
            "title": reminder.title,
            "at": reminder.snoozed_until or reminder.remind_at,
            "status": reminder.status.value,
            "meta": {"recurrence": reminder.recurrence.value},
        }

    @staticmethod
    def _roadmap_item(day) -> dict:  # noqa: ANN001
        return {
            "kind": "roadmap_day",
            "id": day.id,
            "title": day.content or f"Dia {day.day_number}",
            "at": day.day_date,
            "status": day.status.value,
            "meta": {"objective_id": day.objective_id, "day_number": day.day_number},
        }

    async def today(self, user_id: int) -> dict:
        """Summarise the user's current local day."""
        zone = await self._user_zone(user_id)
        local_now = datetime.now(zone)
        day = local_now.date()
        start_utc, end_utc = self._local_day_bounds(day, zone)

        tasks = await self.tasks.list_due_between(user_id, start_utc, end_utc)
        reminders = await self.reminders.list_between(user_id, start_utc, end_utc)
        days = await self.roadmap.list_days_between(user_id, start_utc, end_utc)
        habits = await self.list_habits(user_id, page=1, limit=100)

        habit_items, _ = habits
        habits_done = sum(1 for habit in habit_items if day in habit["check_ins"])
        task_items = [self._task_item(t) for t in tasks]
        reminder_items = [self._reminder_item(r) for r in reminders]
        day_items = [self._roadmap_item(d) for d in days]

        return {
            "date": day,
            "timezone": str(zone),
            "tasks": task_items,
            "reminders": reminder_items,
            "roadmap_days": day_items,
            "habits": habit_items,
            "counts": {
                "tasks": len(task_items),
                "reminders": len(reminder_items),
                "roadmap_days": len(day_items),
                "habits": len(habit_items),
                "habits_done": habits_done,
            },
        }

    # ------------------------------------------------------------------
    # Weekly plan (AI, cache-first)
    # ------------------------------------------------------------------
    async def generate_weekly_plan(self, user_id: int, *, plan_tier: PlanTier, focus: str | None, redis_client) -> dict:  # noqa: ANN001
        if self.ai_client is None:
            raise HTTPException(status_code=503, detail="Serviço de IA indisponível")
        user_message = focus or "Monte meu plano para a semana considerando minhas tarefas e rotina."
        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=_WEEKLY_PLAN_SYSTEM_PROMPT,
                user_message=user_message,
                task_type=AITaskType.STRICT_JSON,
                plan_tier=plan_tier,
                user_id=user_id,
            ),
        )
        parsed = extract_json(assistant.text, context="Weekly plan")
        suggestions = self._parse_suggestions(parsed)
        payload = {"suggestions": suggestions}
        await redis_client.set(
            _WEEKLY_PLAN_CACHE_KEY.format(user_id=user_id),
            json.dumps(payload),
            ex=_WEEKLY_PLAN_TTL_SECONDS,
        )
        return {"suggestions": suggestions, "disclaimer": WEEKLY_PLAN_DISCLAIMER}

    @staticmethod
    def _parse_suggestions(parsed: object) -> list[dict]:
        """Validate/normalise the model's suggestion list (never trust it)."""
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("suggestions")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for entry in raw[:20]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            if not title:
                continue
            kind = "reminder" if str(entry.get("kind", "task")).lower() == "reminder" else "task"
            priority = str(entry.get("priority", "medium")).lower()
            if priority not in {"low", "medium", "high"}:
                priority = "medium"
            out.append(
                {
                    "kind": kind,
                    "title": title[:200],
                    "due_at": entry.get("due_at"),
                    "remind_at": entry.get("remind_at"),
                    "priority": priority,
                    "notes": (str(entry["notes"])[:2000] if entry.get("notes") else None),
                }
            )
        return out

    async def accept_weekly_plan(self, user_id: int, redis_client) -> dict:  # noqa: ANN001
        """Persist the cached weekly-plan proposal and clear the cache.

        Nothing is written until this point: the proposal lives only in Redis
        between ``POST /me/weekly-plan`` and ``POST /me/weekly-plan/accept``.
        """
        key = _WEEKLY_PLAN_CACHE_KEY.format(user_id=user_id)
        raw = await redis_client.get(key)
        if not raw:
            raise HTTPException(status_code=404, detail="Nenhum plano semanal pendente")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        suggestions = data.get("suggestions") if isinstance(data, dict) else None
        if not isinstance(suggestions, list):
            await redis_client.delete(key)
            raise HTTPException(status_code=404, detail="Nenhum plano semanal pendente")

        zone = await self._user_zone(user_id)
        created_tasks = 0
        created_reminders = 0
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            title = str(suggestion.get("title", "")).strip()
            if not title:
                continue
            kind = str(suggestion.get("kind", "task"))
            priority = str(suggestion.get("priority", "medium")).lower()
            if priority not in {"low", "medium", "high"}:
                priority = "medium"
            notes = suggestion.get("notes")
            if kind == "reminder":
                remind_at = _parse_dt(suggestion.get("remind_at") or suggestion.get("due_at"))
                if remind_at is None:
                    continue
                await self.reminders.create(
                    user_id,
                    title=title,
                    body=notes,
                    remind_at=self._as_utc(remind_at, zone),
                    recurrence=Recurrence.NONE,
                )
                created_reminders += 1
            else:
                due_at = _parse_dt(suggestion.get("due_at"))
                await self.tasks.create(
                    user_id,
                    title=title,
                    notes=notes,
                    due_at=self._as_utc(due_at, zone) if due_at else None,
                    priority=TaskPriority(priority),
                    recurrence=Recurrence.NONE,
                )
                created_tasks += 1
        await redis_client.delete(key)
        return {"created_tasks": created_tasks, "created_reminders": created_reminders}
