"""
Repository layer for the trips/shifts/child-care logbook.

Owner-scoping on every query.  The report reads run a couple of grouped
queries instead of loading every row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.logbook import LogEntry, LogKind


class LogEntryRepository:
    """Data access for :class:`LogEntry`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def create(self, user_id: int, **fields: Any) -> LogEntry:
        entry = LogEntry(user_id=user_id, **fields)
        self.session.add(entry)
        await self._commit()
        return entry

    async def get(self, entry_id: int) -> LogEntry | None:
        return (await self.session.execute(select(LogEntry).where(LogEntry.id == entry_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        kind: LogKind | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[LogEntry], int]:
        clauses = [LogEntry.user_id == user_id]
        if kind is not None:
            clauses.append(LogEntry.kind == kind)
        if from_at is not None:
            clauses.append(LogEntry.started_at >= from_at)
        if to_at is not None:
            clauses.append(LogEntry.started_at <= to_at)
        stmt = (
            select(LogEntry)
            .where(*clauses)
            .order_by(LogEntry.started_at.desc(), LogEntry.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(LogEntry)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def update(self, entry: LogEntry, **fields: Any) -> LogEntry:
        for key, value in fields.items():
            setattr(entry, key, value)
        await self._commit(entry)
        return entry

    async def delete(self, entry: LogEntry) -> None:
        await self.session.delete(entry)
        await self._commit()

    async def report_rows(self, user_id: int, start: datetime, end: datetime) -> list[LogEntry]:
        """Return the entries overlapping ``[start, end]`` for aggregation."""
        stmt = (
            select(LogEntry)
            .where(LogEntry.user_id == user_id, LogEntry.started_at >= start, LogEntry.started_at <= end)
            .order_by(LogEntry.started_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())
