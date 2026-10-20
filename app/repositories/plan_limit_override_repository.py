"""
Repository layer for :class:`~app.models.plan_limit_override.PlanLimitOverride`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plan_limit_override import PlanLimitOverride


class PlanLimitOverrideRepository:
    """Data-access layer for per-plan ceiling overrides."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[PlanLimitOverride]:
        """Return every plan override, ordered by plan."""
        result = await self.session.execute(select(PlanLimitOverride).order_by(PlanLimitOverride.plan))
        return list(result.scalars().all())

    async def get(self, plan: str) -> PlanLimitOverride | None:
        """Return the override row for ``plan``, if any."""
        result = await self.session.execute(
            select(PlanLimitOverride).where(PlanLimitOverride.plan == plan)
        )
        return result.scalar_one_or_none()

    async def upsert(self, plan: str, values: dict[str, Any], *, updated_by: int | None) -> PlanLimitOverride:
        """Insert or update the override for ``plan`` with ``values``."""
        row = await self.get(plan)
        if row is None:
            row = PlanLimitOverride(plan=plan)
            self.session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_by = updated_by
        return row
