"""Repository for the public account-deletion request queue."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deletion_request import DeletionRequest


class DeletionRequestRepository:
    """Data access for :class:`~app.models.deletion_request.DeletionRequest`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def get(self, request_id: int) -> DeletionRequest | None:
        """Return one request by id."""
        stmt = select(DeletionRequest).where(DeletionRequest.id == request_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_moderation(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[DeletionRequest], int]:
        """Return a page of deletion requests, newest first."""
        clauses: list[Any] = []
        if status is not None:
            clauses.append(DeletionRequest.status == status)
        stmt = (
            select(DeletionRequest)
            .where(*clauses)
            .order_by(DeletionRequest.requested_at.desc(), DeletionRequest.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(DeletionRequest)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def set_status(self, request: DeletionRequest, status: str, *, note: str | None = None) -> DeletionRequest:
        """Mark a request handled (or back to pending) and record a note."""
        request.status = status
        if note is not None:
            request.note = note
        await self._commit(request)
        return request

    async def set_status_by_id(
        self, request_id: int, status: str, *, note: str | None = None
    ) -> DeletionRequest | None:
        """Load a request by id and set its status; ``None`` when missing."""
        row = await self.get(request_id)
        if row is None:
            return None
        return await self.set_status(row, status, note=note)

    async def count_by_status(self, status: str) -> int:
        """Count requests in one status (sidebar badge)."""
        stmt = (
            select(func.count())
            .select_from(DeletionRequest)
            .where(DeletionRequest.status == status)
        )
        return int((await self.session.execute(stmt)).scalar_one())
