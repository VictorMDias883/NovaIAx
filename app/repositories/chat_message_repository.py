"""Repository for the chat-turn identifier registry.

Only the identifier, owner and originating agent are stored — never the prompt
or the reply — so this table carries no user content.  Its sole purpose is to
prove that a ``message_id`` echoed back by a client (e.g. in an AI-feedback
report) genuinely belongs to the caller.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_message import ChatAgent, ChatMessage


class ChatMessageRepository:
    """Data access for :class:`~app.models.chat_message.ChatMessage`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def create(
        self, user_id: int, *, message_id: str, agent: ChatAgent
    ) -> ChatMessage:
        """Persist one chat-turn identifier for ``user_id``."""
        record = ChatMessage(user_id=user_id, message_id=message_id, agent=agent)
        self.session.add(record)
        await self._commit(record)
        return record

    async def exists_for_user(self, user_id: int, message_id: str) -> bool:
        """Return whether ``message_id`` was issued to ``user_id``."""
        stmt = select(
            exists().where(
                ChatMessage.user_id == user_id,
                ChatMessage.message_id == message_id,
            )
        )
        return bool((await self.session.execute(stmt)).scalar())
