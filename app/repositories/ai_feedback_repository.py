"""Repository for user reports about AI answers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_feedback import AIFeedback


class AIFeedbackRepository:
    """Data access for :class:`~app.models.ai_feedback.AIFeedback`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def create(
        self,
        user_id: int,
        *,
        message_id: str,
        reason: str,
        details: str | None,
        message_excerpt: str | None = None,
    ) -> AIFeedback:
        feedback = AIFeedback(
            user_id=user_id,
            message_id=message_id,
            reason=reason,
            details=details,
            message_excerpt=message_excerpt,
        )
        self.session.add(feedback)
        await self._commit(feedback)
        return feedback

    async def get(self, feedback_id: int) -> AIFeedback | None:
        """Return one report by id."""
        stmt = select(AIFeedback).where(AIFeedback.id == feedback_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_moderation(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[AIFeedback], int]:
        """Return a page of reports for the admin review queue."""
        clauses: list[Any] = []
        if status is not None:
            clauses.append(AIFeedback.status == status)
        stmt = (
            select(AIFeedback)
            .where(*clauses)
            .order_by(AIFeedback.created_at.desc(), AIFeedback.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(AIFeedback)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def set_status(self, feedback: AIFeedback, status: str) -> AIFeedback:
        """Update a report's review status."""
        feedback.status = status
        await self._commit(feedback)
        return feedback

    async def count_by_status(self, status: str) -> int:
        """Count reports in one review status (sidebar badge)."""
        stmt = (
            select(func.count())
            .select_from(AIFeedback)
            .where(AIFeedback.status == status)
        )
        return int((await self.session.execute(stmt)).scalar_one())
