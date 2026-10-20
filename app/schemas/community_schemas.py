"""Pydantic schemas for the community-reporting endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.community import OfficialContactCategory, ReportCategory, ReportStatus


class ReportCreate(BaseModel):
    """Request body for creating a community report."""

    category: ReportCategory = ReportCategory.OTHER
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    location: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    is_anonymous: bool = False


class ReportUpdate(BaseModel):
    """Request body for patching the caller's own report."""

    category: ReportCategory | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=10_000)
    location: str | None = Field(default=None, max_length=200)
    occurred_at: datetime | None = None
    is_anonymous: bool | None = None


class ReportResponse(BaseModel):
    """Response body for a report.

    ``author_id`` is ``None`` for anonymous reports (even for the author).
    ``confirmations`` and ``flags`` are aggregate counters; ``confirmed_by_me``
    tells the caller whether they already confirmed it.
    """

    id: int
    category: ReportCategory
    title: str
    description: str
    location: str | None = None
    occurred_at: datetime | None = None
    status: ReportStatus
    is_anonymous: bool
    author_id: int | None = None
    photo_path: str | None = None
    confirmations: int
    flags: int
    confirmed_by_me: bool
    created_at: datetime
    updated_at: datetime


class ReportListResponse(BaseModel):
    """Paginated report list."""

    items: list[ReportResponse]
    page: int
    limit: int
    total: int


class ConfirmationResponse(BaseModel):
    """Result of confirming (or un-confirming) a report."""

    report_id: int
    confirmed: bool
    confirmations: int


class FlagCreate(BaseModel):
    """Request body for flagging a report."""

    reason: str | None = Field(default=None, max_length=2000)


class FlagResponse(BaseModel):
    """Result of flagging a report."""

    report_id: int
    flagged: bool
    flags: int


class BlockCreate(BaseModel):
    """Request body for blocking another user."""

    blocked_id: int


class BlockResponse(BaseModel):
    """One blocking relationship from the caller's perspective."""

    blocked_id: int
    created_at: datetime


class BlockListResponse(BaseModel):
    """The caller's block list."""

    items: list[BlockResponse]


class OfficialContactResponse(BaseModel):
    """A public-agency contact from the global directory."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    category: OfficialContactCategory
    phone: str
    description: str | None = None
    scope: str | None = None
    region: str | None = None
    url: str | None = None
    source: str | None = None


class OfficialContactListResponse(BaseModel):
    """The official-contact directory (optionally filtered by category)."""

    items: list[OfficialContactResponse]


class SupportResourceResponse(BaseModel):
    """A domestic-violence support resource.

    This endpoint is **read-only**: nothing a caller sends here is stored or
    published.  It exists only to surface help lines.
    """

    name: str
    phone: str
    description: str


class SupportResourceListResponse(BaseModel):
    """Read-only support resources."""

    items: list[SupportResourceResponse]


class DraftCreate(BaseModel):
    """Request body for creating/editing a draft complaint."""

    body: str = Field(min_length=1, max_length=20_000)
    recipient: str | None = Field(default=None, max_length=200)
    report_id: int | None = None


class DraftUpdate(BaseModel):
    """Request body for patching a draft complaint."""

    body: str | None = Field(default=None, min_length=1, max_length=20_000)
    recipient: str | None = Field(default=None, max_length=200)


class DraftGenerateRequest(BaseModel):
    """Request body to generate a draft from a report using the AI."""

    report_id: int


class DraftResponse(BaseModel):
    """A draft complaint.  It is a template the user must review and send."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    report_id: int | None = None
    recipient: str | None = None
    body: str
    disclaimer: str
    created_at: datetime
    updated_at: datetime


class DraftListResponse(BaseModel):
    """Paginated draft list."""

    items: list[DraftResponse]
    page: int
    limit: int
    total: int


class ReportModerationUpdate(BaseModel):
    """Admin moderation action for a report."""

    status: ReportStatus
