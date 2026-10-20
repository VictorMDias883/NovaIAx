"""Service layer for the admin console's account-deletion request queue.

Requests are created by the public ``/account-deletion`` form when the
requester cannot prove ownership with a password.  Operators act on them from
the panel: the one-click *execute* runs the exact same purge as
``DELETE /api/v1/me`` (:class:`app.services.privacy_service.AccountDeletionService`),
only without the password check — identity was established out of band.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.core.logging import get_logger
from app.models.deletion_request import DELETION_STATUS_HANDLED, DELETION_STATUS_PENDING
from app.repositories.deletion_request_repository import DeletionRequestRepository
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

__all__ = ["DeletionRequestService"]


class DeletionRequestService:
    """Read and resolve the queued deletion requests."""

    def __init__(self, session: AsyncSession, redis_client: RedisClient | None = None) -> None:
        self.session = session
        self.repo = DeletionRequestRepository(session)
        self.redis_client = redis_client

    async def list_queue(
        self, *, status: str | None = None, page: int = 1, limit: int = 25
    ) -> tuple[list[dict[str, Any]], int]:
        """Return one page of requests plus the total, newest first."""
        page = max(1, page)
        limit = min(max(1, limit), 100)
        rows, total = await self.repo.list_for_moderation(
            status=status, offset=(page - 1) * limit, limit=limit
        )
        views = [self._view(row) for row in rows]
        await self.session.rollback()
        return views, total

    async def get(self, request_id: int) -> dict[str, Any]:
        """Return one request (404 when unknown)."""
        row = await self.repo.get(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
        view = self._view(row)
        await self.session.rollback()
        return view

    async def execute(self, request_id: int, *, redis_client: RedisClient | None = None) -> dict[str, Any]:
        """Purge the account the request refers to and mark it handled.

        Runs even when no user exists for the e-mail (nothing to delete), so
        the queue row can always be resolved.
        """
        row = await self.repo.get(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
        if row.status != DELETION_STATUS_PENDING:
            raise HTTPException(status_code=400, detail="Esta solicitação já foi tratada.")
        email = row.email
        # Copy the id out and release the read transaction so the purge can
        # own its own ``session.begin()`` (see
        # :func:`app.api.deps._release_read_transaction`).
        await self.session.rollback()

        user = await UserRepository(self.session).get_by_email(email)
        user_id = user.id if user is not None else None
        await self.session.rollback()

        purged = False
        if user_id is not None:
            from app.services.privacy_service import AccountDeletionService

            await AccountDeletionService(
                self.session, redis_client=redis_client or self.redis_client
            ).purge_user(user_id)
            purged = True

        note = (
            f"Conta excluída por {email}." if purged else f"Nenhuma conta com {email}."
        )
        updated = await self.repo.set_status_by_id(
            request_id, DELETION_STATUS_HANDLED, note=note
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
        logger.info(
            "deletion_request_executed",
            extra={"request_id": request_id, "email": email, "purged": purged},
        )
        view = self._view(updated)
        await self.session.rollback()
        return view

    async def mark(
        self, request_id: int, status: str, *, note: str | None = None
    ) -> dict[str, Any]:
        """Set a request's status (used to record out-of-band resolutions)."""
        if status not in (DELETION_STATUS_PENDING, DELETION_STATUS_HANDLED):
            raise HTTPException(status_code=400, detail="Status inválido.")
        row = await self.repo.get(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Solicitação não encontrada.")
        updated = await self.repo.set_status(row, status, note=note)
        view = self._view(updated)
        await self.session.rollback()
        return view

    async def pending_count(self) -> int:
        """Count requests still waiting for a decision (sidebar badge)."""
        count = await self.repo.count_by_status(DELETION_STATUS_PENDING)
        await self.session.rollback()
        return count

    @staticmethod
    def _view(row) -> dict[str, Any]:  # noqa: ANN001
        """Project one row into the panel's view."""
        return {
            "id": row.id,
            "email": row.email,
            "status": row.status,
            "note": row.note,
            "requested_at": row.requested_at,
        }
