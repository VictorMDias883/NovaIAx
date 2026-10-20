"""
SQLAlchemy ORM models for the social-community reporting domain.

Two scopes live here:

* **Owner-scoped** (indexed ``user_id``) — :class:`CommunityReport` (author),
  :class:`ReportConfirmation`, :class:`ReportFlag`, :class:`UserBlock`,
  :class:`DraftComplaint`.
* **Global directory** — :class:`OfficialContact` (public agencies); not owned
  by any user.

Safety invariants (enforced by the service layer, documented here):

* the **domestic-violence support** endpoint is a *read-only* resource list and
  never creates a :class:`CommunityReport` or any other row — nothing a victim
  submits at that endpoint is stored or published;
* report photos are stored with **EXIF stripped** (``photo_path`` only;
  geolocation never leaves the device);
* a draft complaint is a *template the user edits* — the AI does not give legal
  advice and the draft carries a disclaimer.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ReportStatus(str, Enum):
    """Moderation lifecycle of a community report."""

    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FLAGGED = "flagged"
    REMOVED = "removed"


class ReportCategory(str, Enum):
    """Coarse category of a community report."""

    SECURITY = "security"
    INFRASTRUCTURE = "infrastructure"
    ENVIRONMENT = "environment"
    TRANSPORT = "transport"
    HEALTH = "health"
    OTHER = "other"


class OfficialContactCategory(str, Enum):
    """Kind of official contact entry."""

    EMERGENCY = "emergency"
    POLICE = "police"
    HEALTH = "health"
    SUPPORT = "support"
    OTHER = "other"


class CommunityReport(Base):
    """A neighborhood/community report authored by one user.

    Table: ``community_reports``.  ``photo_path`` references an
    already-EXIF-stripped file; the raw upload is never stored.
    """

    __tablename__ = "community_reports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[ReportCategory] = mapped_column(
        SQLEnum(ReportCategory, name="report_category", native_enum=True),
        nullable=False,
        default=ReportCategory.OTHER,
        server_default=ReportCategory.OTHER.name,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        SQLEnum(ReportStatus, name="report_status", native_enum=True),
        nullable=False,
        default=ReportStatus.SUBMITTED,
        server_default=ReportStatus.SUBMITTED.name,
        index=True,
    )
    is_anonymous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    photo_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    #: Set when the report is automatically hidden from the community feed
    #: because it collected ``community_report_flag_threshold`` flags.  A
    #: moderator can restore it.  ``None`` means it is visible normally.
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReportConfirmation(Base):
    """A user confirming a report (one per user per report).

    Table: ``report_confirmations``.
    """

    __tablename__ = "report_confirmations"
    __table_args__ = (UniqueConstraint("report_id", "user_id", name="uq_report_confirmations_report_id_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("community_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ReportFlag(Base):
    """A user flagging a report for moderation (one per user per report).

    Table: ``report_flags``.
    """

    __tablename__ = "report_flags"
    __table_args__ = (UniqueConstraint("report_id", "user_id", name="uq_report_flags_report_id_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("community_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserBlock(Base):
    """A directional block: ``blocker`` no longer sees ``blocked``'s content.

    Table: ``user_blocks``.
    """

    __tablename__ = "user_blocks"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_user_blocks_blocker_id_blocked_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    blocker_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OfficialContact(Base):
    """A public-agency contact (global directory, not owner-scoped).

    Table: ``official_contacts``.
    """

    __tablename__ = "official_contacts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[OfficialContactCategory] = mapped_column(
        SQLEnum(OfficialContactCategory, name="official_contact_category", native_enum=True),
        nullable=False,
        default=OfficialContactCategory.OTHER,
        server_default=OfficialContactCategory.OTHER.name,
        index=True,
    )
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DraftComplaint(Base):
    """A user-editable draft complaint generated from a report.

    Table: ``draft_complaints``.  The body is a template the user must review
    and send themselves; the backend never files it and never gives legal
    advice.
    """

    __tablename__ = "draft_complaints"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("community_reports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    recipient: Mapped[str | None] = mapped_column(String(200), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = [
    "CommunityReport",
    "DraftComplaint",
    "OfficialContact",
    "OfficialContactCategory",
    "ReportCategory",
    "ReportConfirmation",
    "ReportFlag",
    "ReportStatus",
    "UserBlock",
]
