"""Service layer for the admin console's AI-feedback review queue.

The product lets a user report an offensive or wrong AI answer.  Operators
review those reports in the panel: they can resolve or ignore each one, and
may see the offending answer **only** when the reporter chose to attach it to
the report (``message_excerpt``) — the backend never persists chat content on
its own, so nothing else from the user's conversations is ever exposed here.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.ai_feedback import (
    AI_FEEDBACK_STATUS_IGNORED,
    AI_FEEDBACK_STATUS_PENDING,
    AI_FEEDBACK_STATUS_RESOLVED,
    AI_FEEDBACK_STATUSES,
    AIFeedback,
)
from app.repositories.ai_feedback_repository import AIFeedbackRepository

logger = get_logger(__name__)

__all__ = ["AI_FEEDBACK_ACTIONS", "AIFeedbackService"]

#: The review actions the admin panel exposes, mapped to the resulting status.
AI_FEEDBACK_ACTIONS = {
    "resolve": AI_FEEDBACK_STATUS_RESOLVED,
    "ignore": AI_FEEDBACK_STATUS_IGNORED,
}


class AIFeedbackService:
    """Read and transition the AI-feedback review queue."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AIFeedbackRepository(session)

    async def list_queue(
        self, *, status: str | None = None, page: int = 1, limit: int = 25
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of reports plus the total, with light views."""
        page = max(1, page)
        limit = min(max(1, limit), 100)
        if status is not None and status not in AI_FEEDBACK_STATUSES:
            raise HTTPException(status_code=400, detail="Status inválido.")
        rows, total = await self.repo.list_for_moderation(
            status=status, offset=(page - 1) * limit, limit=limit
        )
        views = [self._view(row) for row in rows]
        await self.session.rollback()
        return views, total

    async def get(self, feedback_id: int) -> dict[str, Any]:
        """Return one report (404 when unknown)."""
        row = await self.repo.get(feedback_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Feedback não encontrado.")
        view = self._view(row)
        await self.session.rollback()
        return view

    async def set_status(self, feedback_id: int, status: str) -> dict[str, Any]:
        """Resolve or ignore one report; 404 when it no longer exists."""
        if status not in AI_FEEDBACK_STATUSES:
            raise HTTPException(status_code=400, detail="Status inválido.")
        row = await self.repo.get(feedback_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Feedback não encontrado.")
        before = row.status
        updated = await self.repo.set_status(row, status)
        after = updated.status
        logger.info(
            "ai_feedback_status_changed",
            extra={"feedback_id": feedback_id, "before": before, "after": after},
        )
        view = self._view(updated)
        await self.session.rollback()
        return view

    async def pending_count(self) -> int:
        """Count reports still waiting for review (sidebar badge)."""
        count = await self.repo.count_by_status(AI_FEEDBACK_STATUS_PENDING)
        await self.session.rollback()
        return count

    @staticmethod
    def _view(row: AIFeedback) -> dict[str, Any]:
        """Project one row into the panel's view (never exposes chat history)."""
        return {
            "id": row.id,
            "user_id": row.user_id,
            "message_id": row.message_id,
            "reason": row.reason,
            "details": row.details,
            "message_excerpt": row.message_excerpt,
            "status": row.status,
            "created_at": row.created_at,
        }
