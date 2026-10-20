"""
Aggregate-only dashboard service for the admin console.

Everything the dashboard shows is produced by a ``COUNT``/``GROUP BY`` on the
database — never by loading user rows into memory.  This is the technical
half of the panel's privacy rule: an operator sees *aggregates and metadata*
about the platform, never the content of anyone's chats, notes, health,
finance, study or logbook entries.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_message import ChatMessage
from app.models.community import CommunityReport, ReportStatus
from app.models.subscription import ENTITLED_STATUSES, Subscription
from app.models.system_prompt import SystemPrompt
from app.models.user import User, UserRole


class AdminDashboardService:
    """Read-only, aggregate dashboard counters."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _count(self, model: type, *conditions) -> int:
        stmt = select(func.count()).select_from(model).where(*conditions)
        return int((await self.session.execute(stmt)).scalar_one())

    async def stats(self) -> dict[str, object]:
        """Return every headline counter in one round of aggregate queries."""
        total_users = await self._count(User)
        total_admins = await self._count(User, User.role == UserRole.ADMIN)
        total_prompts = await self._count(SystemPrompt)
        open_reports = await self._count(CommunityReport, CommunityReport.status == ReportStatus.SUBMITTED)
        # ``CURRENT_DATE`` resolves in the database's timezone and compares
        # correctly on both PostgreSQL and the SQLite test backend.
        messages_today = await self._count(ChatMessage, ChatMessage.created_at >= func.current_date())

        plan_stmt = (
            select(Subscription.plan, func.count())
            .where(Subscription.status.in_(tuple(ENTITLED_STATUSES)))
            .group_by(Subscription.plan)
            .order_by(Subscription.plan)
        )
        plan_rows = (await self.session.execute(plan_stmt)).all()
        active_by_plan = [
            {"plan": (plan.value if hasattr(plan, "value") else str(plan)), "count": int(count)}
            for plan, count in plan_rows
        ]

        return {
            "total_users": total_users,
            "total_admins": total_admins,
            "total_prompts": total_prompts,
            "open_reports": open_reports,
            "messages_today": messages_today,
            "active_subscriptions": active_by_plan,
            "active_subscriptions_total": sum(int(item["count"]) for item in active_by_plan),
        }


__all__ = ["AdminDashboardService"]
