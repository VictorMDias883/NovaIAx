"""Pydantic schemas for the trips/shifts/child-care logbook endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.logbook import LogKind


class LogEntryCreate(BaseModel):
    """Request body for creating a log entry."""

    kind: LogKind
    title: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=80)
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)
    metadata: dict[str, Any] | None = None


class LogEntryUpdate(BaseModel):
    """Request body for patching a log entry (all fields optional)."""

    kind: LogKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=80)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)
    metadata: dict[str, Any] | None = None


class LogEntryResponse(BaseModel):
    """Response body for a single log entry.

    ``metadata`` maps to the ORM's ``metadata_`` attribute (the name
    ``metadata`` is reserved by SQLAlchemy's declarative base).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: LogKind
    title: str
    category: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_minutes: int | None = None
    notes: str | None = None
    metadata: dict[str, Any] | None = Field(default=None, validation_alias="metadata_")
    created_at: datetime
    updated_at: datetime


class LogEntryListResponse(BaseModel):
    """Paginated log-entry list."""

    items: list[LogEntryResponse]
    page: int
    limit: int
    total: int


class LogKindReport(BaseModel):
    """Aggregated figures for one log kind."""

    kind: LogKind
    count: int
    total_minutes: int


class LogReportResponse(BaseModel):
    """Report for a date range, grouped by kind and category."""

    from_: datetime = Field(serialization_alias="from")
    to: datetime
    total_entries: int
    total_minutes: int
    by_kind: list[LogKindReport]
    by_category: dict[str, int]
