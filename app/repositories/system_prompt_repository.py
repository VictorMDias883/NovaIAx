"""
Repository layer for the ``SystemPrompt`` model.

This repository provides CRUD operations for administrator-managed
system prompt records.
"""

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_prompt import SystemPrompt


class SystemPromptRepository:
    """Data-access layer for :class:`SystemPrompt` entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, offset: int = 0, limit: int = 100) -> list[SystemPrompt]:
        # Deterministic ordering (id ASC) so pagination results are stable
        # across calls — without an ORDER BY Postgres may return rows in any
        # order, making offset/limit pagination non-deterministic.
        stmt = select(SystemPrompt).order_by(SystemPrompt.id.asc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        stmt = select(func.count()).select_from(SystemPrompt)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def get_by_id(self, prompt_id: int) -> SystemPrompt | None:
        stmt = select(SystemPrompt).where(SystemPrompt.id == prompt_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def create(self, tipo: str, system_prompt: str) -> SystemPrompt:
        prompt = SystemPrompt(tipo=tipo, system_prompt=system_prompt)
        self.session.add(prompt)
        await self.session.commit()
        await self.session.refresh(prompt)
        return prompt

    async def update(self, prompt_id: int, tipo: str, system_prompt: str) -> SystemPrompt | None:
        stmt = (
            update(SystemPrompt)
            .where(SystemPrompt.id == prompt_id)
            .values(tipo=tipo, system_prompt=system_prompt)
            .execution_options(synchronize_session="fetch")
        )
        await self.session.execute(stmt)
        await self.session.commit()
        return await self.get_by_id(prompt_id)

    async def delete(self, prompt_id: int) -> None:
        stmt = delete(SystemPrompt).where(SystemPrompt.id == prompt_id)
        await self.session.execute(stmt)
        await self.session.commit()
