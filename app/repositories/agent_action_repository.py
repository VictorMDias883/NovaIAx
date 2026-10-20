"""Repository for the agent-action audit trail."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action import AgentAction, AgentActionStatus, AgentActionType


class AgentActionRepository:
    """Data access for :class:`~app.models.agent_action.AgentAction`."""

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
        action_type: AgentActionType,
        params: dict[str, Any],
        summary: str,
        requires_confirmation: bool,
        is_device: bool,
        prepares_only: bool,
    ) -> AgentAction:
        action = AgentAction(
            user_id=user_id,
            message_id=message_id,
            type=action_type,
            status=AgentActionStatus.PROPOSED,
            params=params,
            summary=summary,
            requires_confirmation=requires_confirmation,
            is_device=is_device,
            prepares_only=prepares_only,
        )
        self.session.add(action)
        await self._commit(action)
        return action

    async def get(self, action_id: int) -> AgentAction | None:
        return (
            await self.session.execute(select(AgentAction).where(AgentAction.id == action_id))
        ).scalars().first()

    async def set_status(
        self,
        action: AgentAction,
        *,
        status: AgentActionStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> AgentAction:
        action.status = status
        action.result = result
        action.error = error
        action.error_code = error_code
        action.detail = detail
        await self._commit(action)
        return action
