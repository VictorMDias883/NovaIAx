"""
Repository layer for the ``PromptVersion`` model.

Prompt history is append-only: :meth:`add` inserts the next version for an
``(entity, entity_id)`` pair, and :meth:`list_for` returns the versions newest
first for the diff/rollback UI.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.prompt_version import PromptVersion


class PromptVersionRepository:
    """Data-access layer for versioned admin-authored prompts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_version(self, entity: str, entity_id: int) -> int:
        """Return the version number a new snapshot should use (1-based)."""
        stmt = select(func.max(PromptVersion.version)).where(
            PromptVersion.entity == entity, PromptVersion.entity_id == entity_id
        )
        result = await self.session.execute(stmt)
        current = result.scalar_one_or_none()
        return int(current or 0) + 1

    async def add(
        self,
        *,
        entity: str,
        entity_id: int,
        content: str,
        note: str | None = None,
        created_by: int | None = None,
    ) -> PromptVersion:
        """Append a snapshot, auto-incrementing the version number."""
        version = await self.next_version(entity, entity_id)
        row = PromptVersion(
            entity=entity,
            entity_id=entity_id,
            version=version,
            content=content,
            note=note,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def list_for(self, entity: str, entity_id: int) -> list[PromptVersion]:
        """Return every snapshot for an entity, newest version first."""
        stmt = (
            select(PromptVersion)
            .where(PromptVersion.entity == entity, PromptVersion.entity_id == entity_id)
            .order_by(PromptVersion.version.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, version_id: int) -> PromptVersion | None:
        """Return one snapshot by primary key."""
        result = await self.session.execute(select(PromptVersion).where(PromptVersion.id == version_id))
        return result.scalar_one_or_none()

    async def delete_for_entity(self, entity: str, entity_id: int) -> None:
        """Remove every snapshot for an entity (used when the entity is deleted)."""
        rows = await self.list_for(entity, entity_id)
        for row in rows:
            await self.session.delete(row)
        await self.session.commit()
