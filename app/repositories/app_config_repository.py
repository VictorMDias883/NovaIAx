"""
Repository layer for :class:`~app.models.app_config.AppConfig`.

A thin data-access wrapper: the service owns validation and the transaction,
the repository only reads and stages writes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_config import AppConfig


class AppConfigRepository:
    """Data-access layer for remote app-configuration entries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[AppConfig]:
        """Return every stored configuration entry."""
        result = await self.session.execute(select(AppConfig).order_by(AppConfig.key))
        return list(result.scalars().all())

    async def get(self, key: str) -> AppConfig | None:
        """Return the entry for ``key``, if any."""
        result = await self.session.execute(select(AppConfig).where(AppConfig.key == key))
        return result.scalar_one_or_none()

    async def upsert(self, key: str, value: Any, *, updated_by: int | None) -> AppConfig:
        """Insert or update ``key`` and return the persisted row."""
        row = await self.get(key)
        if row is None:
            row = AppConfig(key=key, value=value, updated_by=updated_by)
            self.session.add(row)
        else:
            row.value = value
            row.updated_by = updated_by
        return row

    def stage(self, row: AppConfig) -> None:
        """Stage a new configuration entry for insertion."""
        self.session.add(row)

    async def delete(self, key: str) -> None:
        """Remove the entry for ``key`` (reverting it to the default)."""
        await self.session.execute(delete(AppConfig).where(AppConfig.key == key))
