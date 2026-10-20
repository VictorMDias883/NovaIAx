"""
Repository layer for the personal-productivity domain.

Each repository isolates the SQLAlchemy queries for one model behind a small,
testable interface, following the pattern of the other repositories in this
package.  Every "list" method returns ``(rows, total)`` so the service can
paginate without issuing a second count query of its own; every query is
filtered by ``user_id`` so owner-scoping is enforced at the data-access layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.productivity import (
    Checklist,
    ChecklistItem,
    FocusSession,
    Habit,
    HabitCheckIn,
    Note,
    Reminder,
    Task,
)
from app.models.roadmap_day import RoadmapDay


class _BaseRepository:
    """Shared session plumbing for the productivity repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        """Commit, then refresh any objects whose server-side ``onupdate``
        columns (e.g. ``updated_at``) would otherwise lazy-load after expiry."""
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def _count(self, *where: Any, model: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await self.session.execute(stmt)).scalar_one())


class TaskRepository(_BaseRepository):
    """Data access for :class:`Task`."""

    async def create(self, user_id: int, **fields: Any) -> Task:
        task = Task(user_id=user_id, **fields)
        self.session.add(task)
        await self._commit()
        return task

    async def get(self, task_id: int) -> Task | None:
        return (await self.session.execute(select(Task).where(Task.id == task_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        status: Any = None,
        priority: Any = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Task], int]:
        clauses = [Task.user_id == user_id]
        if status is not None:
            clauses.append(Task.status == status)
        if priority is not None:
            clauses.append(Task.priority == priority)
        if due_from is not None:
            clauses.append(Task.due_at >= due_from)
        if due_to is not None:
            clauses.append(Task.due_at <= due_to)
        stmt = (
            select(Task)
            .where(*clauses)
            .order_by(Task.due_at.is_(None), Task.due_at, Task.id)
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Task)
        return rows, total

    async def list_due_between(self, user_id: int, start: datetime, end: datetime) -> list[Task]:
        stmt = (
            select(Task).where(Task.user_id == user_id, Task.due_at >= start, Task.due_at <= end).order_by(Task.due_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_status(self, user_id: int) -> dict[str, int]:
        stmt = select(Task.status, func.count()).where(Task.user_id == user_id).group_by(Task.status)
        rows = (await self.session.execute(stmt)).all()
        return {str(status.value if hasattr(status, "value") else status): int(count) for status, count in rows}

    async def update(self, task: Task, **fields: Any) -> Task:
        for key, value in fields.items():
            setattr(task, key, value)
        await self._commit(task)
        return task

    async def delete(self, task: Task) -> None:
        await self.session.delete(task)
        await self._commit()


class ReminderRepository(_BaseRepository):
    """Data access for :class:`Reminder`."""

    async def create(self, user_id: int, **fields: Any) -> Reminder:
        reminder = Reminder(user_id=user_id, **fields)
        self.session.add(reminder)
        await self._commit()
        return reminder

    async def get(self, reminder_id: int) -> Reminder | None:
        return (await self.session.execute(select(Reminder).where(Reminder.id == reminder_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        status: Any = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Reminder], int]:
        clauses = [Reminder.user_id == user_id]
        if status is not None:
            clauses.append(Reminder.status == status)
        if from_at is not None:
            clauses.append(Reminder.remind_at >= from_at)
        if to_at is not None:
            clauses.append(Reminder.remind_at <= to_at)
        stmt = select(Reminder).where(*clauses).order_by(Reminder.remind_at).offset(offset).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Reminder)
        return rows, total

    async def list_between(self, user_id: int, start: datetime, end: datetime) -> list[Reminder]:
        stmt = (
            select(Reminder)
            .where(Reminder.user_id == user_id, Reminder.remind_at >= start, Reminder.remind_at <= end)
            .order_by(Reminder.remind_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def update(self, reminder: Reminder, **fields: Any) -> Reminder:
        for key, value in fields.items():
            setattr(reminder, key, value)
        await self._commit(reminder)
        return reminder

    async def delete(self, reminder: Reminder) -> None:
        await self.session.delete(reminder)
        await self._commit()


class NoteRepository(_BaseRepository):
    """Data access for :class:`Note`."""

    async def create(self, user_id: int, **fields: Any) -> Note:
        note = Note(user_id=user_id, **fields)
        self.session.add(note)
        await self._commit()
        return note

    async def get(self, note_id: int) -> Note | None:
        return (await self.session.execute(select(Note).where(Note.id == note_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        tag: str | None = None,
        pinned: bool | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Note], int]:
        clauses = [Note.user_id == user_id]
        if tag:
            # ``tags`` is a JSON-encoded text column; cast to plain text so the
            # LIKE pattern is not run through the JSONList bind processor.
            clauses.append(cast(Note.tags, String).like(f'%"{tag}"%'))
        if pinned is not None:
            clauses.append(Note.pinned.is_(pinned))
        stmt = (
            select(Note)
            .where(*clauses)
            .order_by(Note.pinned.desc(), Note.updated_at.desc(), Note.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Note)
        return rows, total

    async def update(self, note: Note, **fields: Any) -> Note:
        for key, value in fields.items():
            setattr(note, key, value)
        await self._commit(note)
        return note

    async def delete(self, note: Note) -> None:
        await self.session.delete(note)
        await self._commit()


class ChecklistRepository(_BaseRepository):
    """Data access for :class:`Checklist` and :class:`ChecklistItem`."""

    async def create(self, user_id: int, **fields: Any) -> Checklist:
        checklist = Checklist(user_id=user_id, **fields)
        self.session.add(checklist)
        await self._commit()
        return checklist

    async def get(self, checklist_id: int) -> Checklist | None:
        return (await self.session.execute(select(Checklist).where(Checklist.id == checklist_id))).scalars().first()

    async def list_items(self, checklist_id: int) -> list[ChecklistItem]:
        stmt = (
            select(ChecklistItem)
            .where(ChecklistItem.checklist_id == checklist_id)
            .order_by(ChecklistItem.position, ChecklistItem.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(
        self,
        user_id: int,
        *,
        category: Any = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Checklist], int]:
        clauses = [Checklist.user_id == user_id]
        if category is not None:
            clauses.append(Checklist.category == category)
        stmt = (
            select(Checklist)
            .where(*clauses)
            .order_by(Checklist.created_at.desc(), Checklist.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Checklist)
        return rows, total

    async def update(self, checklist: Checklist, **fields: Any) -> Checklist:
        for key, value in fields.items():
            setattr(checklist, key, value)
        await self._commit(checklist)
        return checklist

    async def delete(self, checklist: Checklist) -> None:
        await self.session.delete(checklist)
        await self._commit()

    async def add_item(self, checklist_id: int, **fields: Any) -> ChecklistItem:
        item = ChecklistItem(checklist_id=checklist_id, **fields)
        self.session.add(item)
        await self._commit()
        return item

    async def get_item(self, item_id: int) -> ChecklistItem | None:
        return (await self.session.execute(select(ChecklistItem).where(ChecklistItem.id == item_id))).scalars().first()

    async def update_item(self, item: ChecklistItem, **fields: Any) -> ChecklistItem:
        for key, value in fields.items():
            setattr(item, key, value)
        await self._commit(item)
        return item

    async def delete_item(self, item: ChecklistItem) -> None:
        await self.session.delete(item)
        await self._commit()


class HabitRepository(_BaseRepository):
    """Data access for :class:`Habit` and :class:`HabitCheckIn`."""

    async def create(self, user_id: int, title: str) -> Habit:
        habit = Habit(user_id=user_id, title=title)
        self.session.add(habit)
        await self._commit()
        return habit

    async def get(self, habit_id: int) -> Habit | None:
        return (await self.session.execute(select(Habit).where(Habit.id == habit_id))).scalars().first()

    async def list_for_user(self, user_id: int, *, offset: int = 0, limit: int = 20) -> tuple[list[Habit], int]:
        clauses = [Habit.user_id == user_id]
        stmt = (
            select(Habit).where(*clauses).order_by(Habit.created_at.desc(), Habit.id.desc()).offset(offset).limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Habit)
        return rows, total

    async def delete(self, habit: Habit) -> None:
        await self.session.delete(habit)
        await self._commit()

    async def list_check_dates(self, habit_id: int) -> list[date]:
        stmt = (
            select(HabitCheckIn.check_date)
            .where(HabitCheckIn.habit_id == habit_id)
            .order_by(HabitCheckIn.check_date.desc())
        )
        return [row for row in (await self.session.execute(stmt)).scalars().all()]

    async def get_check(self, habit_id: int, check_date: date) -> HabitCheckIn | None:
        stmt = select(HabitCheckIn).where(HabitCheckIn.habit_id == habit_id, HabitCheckIn.check_date == check_date)
        return (await self.session.execute(stmt)).scalars().first()

    async def add_check(self, habit_id: int, check_date: date) -> HabitCheckIn:
        check = HabitCheckIn(habit_id=habit_id, check_date=check_date)
        self.session.add(check)
        await self._commit()
        return check


class FocusSessionRepository(_BaseRepository):
    """Data access for :class:`FocusSession`."""

    async def create(self, user_id: int, **fields: Any) -> FocusSession:
        session = FocusSession(user_id=user_id, **fields)
        self.session.add(session)
        await self._commit()
        return session

    async def get(self, session_id: int) -> FocusSession | None:
        return (await self.session.execute(select(FocusSession).where(FocusSession.id == session_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[FocusSession], int]:
        clauses = [FocusSession.user_id == user_id]
        if from_at is not None:
            clauses.append(FocusSession.start_at >= from_at)
        if to_at is not None:
            clauses.append(FocusSession.start_at <= to_at)
        stmt = select(FocusSession).where(*clauses).order_by(FocusSession.start_at.desc()).offset(offset).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=FocusSession)
        return rows, total

    async def update(self, session_obj: FocusSession, **fields: Any) -> FocusSession:
        for key, value in fields.items():
            setattr(session_obj, key, value)
        await self._commit(session_obj)
        return session_obj

    async def delete(self, session_obj: FocusSession) -> None:
        await self.session.delete(session_obj)
        await self._commit()


class RoadmapAgendaRepository(_BaseRepository):
    """Read-only queries merging roadmap days into the agenda."""

    async def list_days_between(self, user_id: int, start: datetime, end: datetime) -> list[RoadmapDay]:
        from app.models.objective import Objective

        stmt = (
            select(RoadmapDay)
            .join(Objective, RoadmapDay.objective_id == Objective.id)
            .where(
                Objective.user_id == user_id,
                RoadmapDay.day_date >= start,
                RoadmapDay.day_date <= end,
            )
            .order_by(RoadmapDay.day_date)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class HabitDailyRepository(_BaseRepository):
    """Read-only helper listing habits checked on a given day."""

    async def list_checked_habit_ids(self, habit_ids: Sequence[int], check_date: date) -> set[int]:
        if not habit_ids:
            return set()
        stmt = select(HabitCheckIn.habit_id).where(
            HabitCheckIn.habit_id.in_(list(habit_ids)),
            HabitCheckIn.check_date == check_date,
        )
        return set((await self.session.execute(stmt)).scalars().all())
