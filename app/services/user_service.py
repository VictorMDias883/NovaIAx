from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.demote_user_command import DemoteUserCommand
from app.commands.delete_user_command import DeleteUserCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.core.logging import get_logger
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)


class UserService:
    """Business logic for user administration and role management."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    async def promote(self, actor: dict[str, Any], command: PromoteUserCommand) -> dict[str, str]:
        """Promote a regular user to administrator."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="User not found")

            if target.role == UserRole.ADMIN:
                return {"message": "User is already an administrator."}

            target.role = UserRole.ADMIN
            target.refresh_token_valid_after = datetime.now(timezone.utc)
            await self.session.flush()
            await self.session.refresh(target)

        logger.info(
            "User promoted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User promoted to administrator."}

    async def demote(self, actor: dict[str, Any], command: DemoteUserCommand) -> dict[str, str]:
        """Demote an administrator to a regular user."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="User not found")

            if target.role != UserRole.ADMIN:
                return {"message": "User is already a regular user."}

            admin_count = await self.repo.count_admins(for_update=True)
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last administrator.")

            target.role = UserRole.USER
            target.refresh_token_valid_after = datetime.now(timezone.utc)
            await self.session.flush()
            await self.session.refresh(target)

        logger.info(
            "User demoted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User demoted to regular user."}

    async def delete(self, actor: dict[str, Any], command: DeleteUserCommand) -> dict[str, str]:
        """Delete a user account by ID."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="User not found")

            if target.role == UserRole.ADMIN:
                admin_count = await self.repo.count_admins(for_update=True)
                if admin_count <= 1:
                    raise HTTPException(status_code=400, detail="Cannot delete the last administrator.")

            await self.session.delete(target)

        logger.info(
            "User deleted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User deleted."}

    async def list_users(self, page: int = 1, limit: int = 20) -> dict[str, Any]:
        """Return a paginated list of users without sensitive fields."""
        page = max(1, page)
        limit = min(max(1, limit), 100)

        stmt = select(User).order_by(User.id).offset((page - 1) * limit).limit(limit)
        result = await self.session.execute(stmt)
        users = result.scalars().all()

        total_result = await self.session.execute(select(func.count()).select_from(User))
        total = total_result.scalar_one()

        return {
            "users": [
                {
                    "id": user.id,
                    "full_name": user.full_name,
                    "email": user.email,
                    "role": user.role.value,
                    "created_at": user.created_at,
                }
                for user in users
            ],
            "page": page,
            "limit": limit,
            "total": total,
        }

    def _ensure_admin(self, actor: dict[str, Any]) -> None:
        if str(actor.get("role")) != UserRole.ADMIN.value:
            raise HTTPException(status_code=403, detail="Not authorized")
