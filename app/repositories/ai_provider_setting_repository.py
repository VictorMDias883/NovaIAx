"""
Repository layer for :class:`~app.models.ai_provider_setting.AIProviderSetting`.

Only the runtime knobs (enabled, priority) are stored here — never a
credential.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_provider_setting import AIProviderSetting


class AIProviderSettingRepository:
    """Data-access layer for AI provider runtime settings."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[AIProviderSetting]:
        """Return every provider setting row."""
        result = await self.session.execute(select(AIProviderSetting).order_by(AIProviderSetting.name))
        return list(result.scalars().all())

    async def get(self, name: str) -> AIProviderSetting | None:
        """Return the setting row for provider ``name``, if any."""
        result = await self.session.execute(
            select(AIProviderSetting).where(AIProviderSetting.name == name)
        )
        return result.scalar_one_or_none()

    async def upsert(self, name: str, *, enabled: bool, priority: int, updated_by: int | None) -> AIProviderSetting:
        """Insert or update the runtime settings for provider ``name``."""
        row = await self.get(name)
        if row is None:
            row = AIProviderSetting(name=name, enabled=enabled, priority=priority, updated_by=updated_by)
            self.session.add(row)
        else:
            row.enabled = enabled
            row.priority = priority
            row.updated_by = updated_by
        return row
