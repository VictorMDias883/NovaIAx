"""
Repository layer for ``UserMemory`` (long-lived per-user facts).

Keys are unique per user.  Explicit user memories are created with
``source="user"`` (a duplicate key is a conflict); auto-learned memories use
the reserved ``auto_`` key prefix and are upserted silently so a repeated
statement refreshes the fact instead of erroring.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_memory import UserMemory


class UserMemoryRepository:
    """Data-access layer for :class:`UserMemory` entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: int, enabled_only: bool = False) -> list[UserMemory]:
        """Return the user's memories, most recently created first."""
        stmt = select(UserMemory).where(UserMemory.user_id == user_id)
        if enabled_only:
            stmt = stmt.where(UserMemory.enabled.is_(True))
        stmt = stmt.order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, user_id: int, key: str) -> UserMemory | None:
        """Return one memory by ``(user_id, key)``."""
        result = await self.session.execute(
            select(UserMemory).where(UserMemory.user_id == user_id, UserMemory.key == key)
        )
        return result.scalar_one_or_none()

    async def create(self, user_id: int, key: str, value: str, source: str = "user") -> UserMemory:
        """Create and persist a memory, returning the created row."""
        memory = UserMemory(user_id=user_id, key=key, value=value, source=source)
        self.session.add(memory)
        await self.session.flush()
        await self.session.refresh(memory)
        return memory

    async def upsert_learned(self, user_id: int, key: str, value: str) -> UserMemory:
        """Create or refresh an auto-learned memory (``source="learned"``).

        ``key`` must carry the reserved ``auto_`` prefix so automatic learning
        can never overwrite an explicitly saved memory.
        """
        existing = await self.get(user_id, key)
        if existing is not None:
            existing.value = value
            existing.source = "learned"
            existing.enabled = True
            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        return await self.create(user_id, key, value, source="learned")

    async def update(self, memory: UserMemory, *, value: str | None = None, enabled: bool | None = None) -> UserMemory:
        """Patch an in-memory row's ``value``/``enabled`` and persist it."""
        if value is not None:
            memory.value = value
        if enabled is not None:
            memory.enabled = enabled
        await self.session.flush()
        await self.session.refresh(memory)
        return memory

    async def delete(self, user_id: int, key: str) -> bool:
        """Delete a memory; return ``True`` when a row was removed."""
        memory = await self.get(user_id, key)
        if memory is None:
            return False
        await self.session.delete(memory)
        return True
