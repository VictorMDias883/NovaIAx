"""
Service layer for the trips/shifts/child-care logbook.

Business rules:

* every entry is owner-scoped;
* an entry may be in progress (``ended_at`` is ``None``); when both ends are
  present, ``ended_at`` must not precede ``started_at``;
* reports are computed on read from the recorded entries, so correcting an
  entry is immediately reflected — nothing is cached or persisted.

    Router → Service → Repository → Database
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.logbook import LogEntry, LogKind
from app.repositories.logbook_repository import LogEntryRepository


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class LogbookService:
    """CRUD and reporting for the caller's own logbook."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.entries = LogEntryRepository(session)

    @staticmethod
    def _validate_span(started_at: datetime, ended_at: datetime | None) -> None:
        if ended_at is not None and ended_at < started_at:
            raise HTTPException(status_code=422, detail="O término não pode ser anterior ao início")

    async def create_entry(self, user_id: int, payload) -> LogEntry:  # noqa: ANN001
        started_at = _as_utc(payload.started_at)
        ended_at = _as_utc(payload.ended_at)
        assert started_at is not None  # required by the schema
        self._validate_span(started_at, ended_at)
        return await self.entries.create(
            user_id,
            kind=payload.kind,
            title=payload.title,
            category=payload.category,
            started_at=started_at,
            ended_at=ended_at,
            notes=payload.notes,
            metadata_=payload.metadata,
        )

    async def list_entries(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        kind: LogKind | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> tuple[list[LogEntry], int]:
        return await self.entries.list_for_user(
            user_id,
            kind=kind,
            from_at=_as_utc(from_at),
            to_at=_as_utc(to_at),
            offset=(page - 1) * limit,
            limit=limit,
        )

    async def _owned(self, user_id: int, entry_id: int) -> LogEntry:
        entry = await self.entries.get(entry_id)
        if entry is None or entry.user_id != user_id:
            raise HTTPException(status_code=404, detail="Registro não encontrado")
        return entry

    async def get_entry(self, user_id: int, entry_id: int) -> LogEntry:
        return await self._owned(user_id, entry_id)

    async def update_entry(self, user_id: int, entry_id: int, payload) -> LogEntry:  # noqa: ANN001
        entry = await self._owned(user_id, entry_id)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("started_at") is not None:
            fields["started_at"] = _as_utc(fields["started_at"])
        if "ended_at" in fields:
            fields["ended_at"] = _as_utc(fields["ended_at"])
        if "metadata" in fields:
            fields["metadata_"] = fields.pop("metadata")
        started_at = fields.get("started_at", entry.started_at)
        ended_at = fields.get("ended_at", entry.ended_at)
        self._validate_span(started_at, ended_at)
        return await self.entries.update(entry, **fields)

    async def delete_entry(self, user_id: int, entry_id: int) -> None:
        await self.entries.delete(await self._owned(user_id, entry_id))

    async def report(self, user_id: int, start: datetime, end: datetime) -> dict:
        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        assert start_utc is not None and end_utc is not None
        rows = await self.entries.report_rows(user_id, start_utc, end_utc)

        by_kind: dict[LogKind, dict[str, int]] = {
            kind: {"count": 0, "total_minutes": 0} for kind in LogKind
        }
        by_category: dict[str, int] = {}
        total_minutes = 0
        for entry in rows:
            bucket = by_kind[entry.kind]
            bucket["count"] += 1
            minutes = entry.duration_minutes
            if minutes is not None:
                bucket["total_minutes"] += minutes
                total_minutes += minutes
            key = entry.category or "sem categoria"
            by_category[key] = by_category.get(key, 0) + 1

        return {
            "from_": start_utc,
            "to": end_utc,
            "total_entries": len(rows),
            "total_minutes": total_minutes,
            "by_kind": [
                {"kind": kind, "count": data["count"], "total_minutes": data["total_minutes"]}
                for kind, data in by_kind.items()
            ],
            "by_category": by_category,
        }
