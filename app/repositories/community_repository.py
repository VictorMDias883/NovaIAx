"""
Repository layer for the community-reporting domain.

Owner-scoping applies to the per-user tables (reports, confirmations, flags,
blocks, drafts).  ``official_contacts`` is a global directory managed from the
admin panel and read by the community/emergency features.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.community import (
    CommunityReport,
    DraftComplaint,
    OfficialContact,
    OfficialContactCategory,
    ReportCategory,
    ReportConfirmation,
    ReportFlag,
    ReportStatus,
    UserBlock,
)


class _BaseRepository:
    """Shared session plumbing for the community repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)


class CommunityReportRepository(_BaseRepository):
    """Data access for :class:`CommunityReport`."""

    async def create(self, user_id: int, **fields: Any) -> CommunityReport:
        report = CommunityReport(user_id=user_id, **fields)
        self.session.add(report)
        await self._commit()
        return report

    async def get(self, report_id: int) -> CommunityReport | None:
        return (
            await self.session.execute(select(CommunityReport).where(CommunityReport.id == report_id))
        ).scalars().first()

    async def list_visible(
        self,
        user_id: int,
        *,
        category: ReportCategory | None = None,
        status: ReportStatus | None = None,
        excluded_user_ids: set[int] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[CommunityReport], int]:
        clauses = [
            CommunityReport.status != ReportStatus.REMOVED,
            CommunityReport.hidden_at.is_(None),
        ]
        if status is not None:
            clauses = [CommunityReport.status == status, CommunityReport.hidden_at.is_(None)]
        if category is not None:
            clauses.append(CommunityReport.category == category)
        if excluded_user_ids:
            clauses.append(CommunityReport.user_id.not_in(excluded_user_ids))
        stmt = (
            select(CommunityReport)
            .where(*clauses)
            .order_by(CommunityReport.created_at.desc(), CommunityReport.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(CommunityReport)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def list_for_moderation(
        self,
        *,
        status: ReportStatus | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[CommunityReport], int]:
        clauses: list[Any] = []
        if status is not None:
            clauses.append(CommunityReport.status == status)
        # Auto-hidden and flagged reports surface first so a moderator sees the
        # urgent rows without paging.  ``hidden_at IS NULL`` sorts ascending, so
        # hidden rows (False) come before visible ones (True).
        stmt = (
            select(CommunityReport)
            .where(*clauses)
            .order_by(
                CommunityReport.hidden_at.is_(None),
                (CommunityReport.status == ReportStatus.FLAGGED).desc(),
                CommunityReport.created_at.desc(),
                CommunityReport.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(CommunityReport)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def set_hidden(self, report: CommunityReport, hidden_at: datetime | None) -> CommunityReport:
        """Stamp or clear the report's auto-hidden marker."""
        report.hidden_at = hidden_at
        await self._commit(report)
        return report

    async def update(self, report: CommunityReport, **fields: Any) -> CommunityReport:
        for key, value in fields.items():
            setattr(report, key, value)
        await self._commit(report)
        return report

    async def delete(self, report: CommunityReport) -> None:
        await self.session.delete(report)
        await self._commit()


class ReportConfirmationRepository(_BaseRepository):
    """Data access for :class:`ReportConfirmation`."""

    async def create(self, report_id: int, user_id: int) -> ReportConfirmation:
        confirmation = ReportConfirmation(report_id=report_id, user_id=user_id)
        self.session.add(confirmation)
        await self._commit()
        return confirmation

    async def get(self, report_id: int, user_id: int) -> ReportConfirmation | None:
        stmt = select(ReportConfirmation).where(
            ReportConfirmation.report_id == report_id, ReportConfirmation.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def delete(self, confirmation: ReportConfirmation) -> None:
        await self.session.delete(confirmation)
        await self._commit()

    async def count_for_report(self, report_id: int) -> int:
        stmt = select(func.count()).select_from(ReportConfirmation).where(ReportConfirmation.report_id == report_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def counts_for_reports(self, report_ids: list[int]) -> dict[int, int]:
        if not report_ids:
            return {}
        stmt = (
            select(ReportConfirmation.report_id, func.count())
            .where(ReportConfirmation.report_id.in_(report_ids))
            .group_by(ReportConfirmation.report_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {int(report_id): int(count) for report_id, count in rows}

    async def report_ids_confirmed_by_user(self, report_ids: list[int], user_id: int) -> set[int]:
        """Return report ids in the page confirmed by ``user_id`` in one query."""
        if not report_ids:
            return set()
        stmt = select(ReportConfirmation.report_id).where(
            ReportConfirmation.report_id.in_(report_ids),
            ReportConfirmation.user_id == user_id,
        )
        return {int(report_id) for report_id in (await self.session.execute(stmt)).scalars().all()}


class ReportFlagRepository(_BaseRepository):
    """Data access for :class:`ReportFlag`."""

    async def create(self, report_id: int, user_id: int, reason: str | None) -> ReportFlag:
        flag = ReportFlag(report_id=report_id, user_id=user_id, reason=reason)
        self.session.add(flag)
        await self._commit()
        return flag

    async def get(self, report_id: int, user_id: int) -> ReportFlag | None:
        stmt = select(ReportFlag).where(ReportFlag.report_id == report_id, ReportFlag.user_id == user_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def count_for_report(self, report_id: int) -> int:
        stmt = select(func.count()).select_from(ReportFlag).where(ReportFlag.report_id == report_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_for_report(self, report_id: int) -> list[ReportFlag]:
        """Return every flag on a report, newest first."""
        stmt = (
            select(ReportFlag)
            .where(ReportFlag.report_id == report_id)
            .order_by(ReportFlag.created_at.desc(), ReportFlag.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete_for_report(self, report_id: int) -> int:
        """Remove every flag on a report; return how many were deleted."""
        flags = await self.list_for_report(report_id)
        for flag in flags:
            await self.session.delete(flag)
        await self._commit()
        return len(flags)

    async def counts_for_reports(self, report_ids: list[int]) -> dict[int, int]:
        """Return the flag count for each report id in one query."""
        if not report_ids:
            return {}
        stmt = (
            select(ReportFlag.report_id, func.count())
            .where(ReportFlag.report_id.in_(report_ids))
            .group_by(ReportFlag.report_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {int(report_id): int(count) for report_id, count in rows}


class UserBlockRepository(_BaseRepository):
    """Data access for :class:`UserBlock`."""

    async def create(self, blocker_id: int, blocked_id: int) -> UserBlock:
        block = UserBlock(blocker_id=blocker_id, blocked_id=blocked_id)
        self.session.add(block)
        await self._commit()
        return block

    async def get(self, blocker_id: int, blocked_id: int) -> UserBlock | None:
        stmt = select(UserBlock).where(UserBlock.blocker_id == blocker_id, UserBlock.blocked_id == blocked_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_blocked_ids(self, blocker_id: int) -> set[int]:
        stmt = select(UserBlock.blocked_id).where(UserBlock.blocker_id == blocker_id)
        return set((await self.session.execute(stmt)).scalars().all())

    async def delete(self, block: UserBlock) -> None:
        await self.session.delete(block)
        await self._commit()


class OfficialContactRepository(_BaseRepository):
    """Data access for the global :class:`OfficialContact` directory."""

    async def list_all(
        self,
        *,
        category: OfficialContactCategory | None = None,
        region: str | None = None,
    ) -> list[OfficialContact]:
        clauses: list[Any] = []
        if category is not None:
            clauses.append(OfficialContact.category == category)
        if region:
            clauses.append(OfficialContact.region == region)
        stmt = select(OfficialContact).where(*clauses).order_by(OfficialContact.category, OfficialContact.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, contact_id: int) -> OfficialContact | None:
        return (
            await self.session.execute(select(OfficialContact).where(OfficialContact.id == contact_id))
        ).scalars().first()

    async def create(self, **fields: Any) -> OfficialContact:
        contact = OfficialContact(**fields)
        self.session.add(contact)
        await self._commit()
        return contact

    async def update(self, contact: OfficialContact, **fields: Any) -> OfficialContact:
        for key, value in fields.items():
            setattr(contact, key, value)
        await self._commit(contact)
        return contact

    async def delete(self, contact: OfficialContact) -> None:
        await self.session.delete(contact)
        await self._commit()


class DraftComplaintRepository(_BaseRepository):
    """Data access for :class:`DraftComplaint`."""

    async def create(self, user_id: int, **fields: Any) -> DraftComplaint:
        draft = DraftComplaint(user_id=user_id, **fields)
        self.session.add(draft)
        await self._commit()
        return draft

    async def get(self, draft_id: int) -> DraftComplaint | None:
        return (
            await self.session.execute(select(DraftComplaint).where(DraftComplaint.id == draft_id))
        ).scalars().first()

    async def list_for_user(self, user_id: int, *, offset: int = 0, limit: int = 20) -> tuple[list[DraftComplaint], int]:
        clauses = [DraftComplaint.user_id == user_id]
        stmt = (
            select(DraftComplaint)
            .where(*clauses)
            .order_by(DraftComplaint.updated_at.desc(), DraftComplaint.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(DraftComplaint)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def update(self, draft: DraftComplaint, **fields: Any) -> DraftComplaint:
        for key, value in fields.items():
            setattr(draft, key, value)
        await self._commit(draft)
        return draft

    async def delete(self, draft: DraftComplaint) -> None:
        await self.session.delete(draft)
        await self._commit()
