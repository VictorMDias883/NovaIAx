"""
API v1 trips/shifts/child-care logbook endpoints.

All routes live under ``/me/logs`` and operate only on the caller's own rows.
The report is computed on read from the recorded entries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_db_user
from app.models.logbook import LogKind
from app.schemas.logbook_schemas import (
    LogEntryCreate,
    LogEntryListResponse,
    LogEntryResponse,
    LogEntryUpdate,
    LogReportResponse,
)
from app.services.logbook_service import LogbookService
from app.utils.request import current_user_id

router = APIRouter(prefix="/me/logs", tags=["logbook"])


@router.get("", response_model=LogEntryListResponse)
async def list_entries(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    kind: LogKind | None = None,
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LogEntryListResponse:
    rows, total = await LogbookService(session).list_entries(
        current_user_id(current_user), page=page, limit=limit, kind=kind, from_at=from_at, to_at=to_at
    )
    return LogEntryListResponse(
        items=[LogEntryResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("", response_model=LogEntryResponse, status_code=201)
async def create_entry(
    payload: LogEntryCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LogEntryResponse:
    entry = await LogbookService(session).create_entry(current_user_id(current_user), payload)
    return LogEntryResponse.model_validate(entry)


@router.get("/report", response_model=LogReportResponse)
async def log_report(
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LogReportResponse:
    start = from_at or (datetime.now(UTC) - timedelta(days=30))
    end = to_at or datetime.now(UTC)
    data = await LogbookService(session).report(current_user_id(current_user), start, end)
    return LogReportResponse(**data)


@router.get("/{entry_id}", response_model=LogEntryResponse)
async def get_entry(
    entry_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LogEntryResponse:
    entry = await LogbookService(session).get_entry(current_user_id(current_user), entry_id)
    return LogEntryResponse.model_validate(entry)


@router.patch("/{entry_id}", response_model=LogEntryResponse)
async def update_entry(
    payload: LogEntryUpdate,
    entry_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LogEntryResponse:
    entry = await LogbookService(session).update_entry(current_user_id(current_user), entry_id, payload)
    return LogEntryResponse.model_validate(entry)


@router.delete("/{entry_id}", status_code=204)
async def delete_entry(
    entry_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await LogbookService(session).delete_entry(current_user_id(current_user), entry_id)
