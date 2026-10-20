"""
Repository layer for :class:`~app.models.promotion.Promotion`.

The public endpoint reads only the active, in-window cards; the admin console
reads and edits every row.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promotion import Promotion


class PromotionRepository:
    """Data-access layer for informational promotion cards."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self, now: datetime) -> list[Promotion]:
        """Return enabled cards whose window includes ``now``, best first."""
        stmt = (
            select(Promotion)
            .where(
                Promotion.active.is_(True),
                or_(Promotion.starts_at.is_(None), Promotion.starts_at <= now),
                or_(Promotion.ends_at.is_(None), Promotion.ends_at >= now),
            )
            .order_by(Promotion.priority.desc(), Promotion.id.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, promotion_id: int) -> Promotion | None:
        """Return the promotion with ``promotion_id``, if any."""
        return await self.session.get(Promotion, promotion_id)

    async def list(
        self, *, include_inactive: bool = True, limit: int = 100, offset: int = 0
    ) -> list[Promotion]:
        """Return promotions ordered by priority then newest first."""
        stmt = select(Promotion)
        if not include_inactive:
            stmt = stmt.where(Promotion.active.is_(True))
        stmt = (
            stmt.order_by(Promotion.priority.desc(), Promotion.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, *, include_inactive: bool = True) -> int:
        """Return the total number of promotions."""
        stmt = select(func.count()).select_from(Promotion)
        if not include_inactive:
            stmt = stmt.where(Promotion.active.is_(True))
        return int((await self.session.execute(stmt)).scalar_one())

    def add(self, promotion: Promotion) -> None:
        """Stage a new promotion for insertion."""
        self.session.add(promotion)

    def update_fields(self, promotion: Promotion, values: dict[str, Any]) -> None:
        """Apply ``values`` to ``promotion`` in the current session."""
        for key, value in values.items():
            setattr(promotion, key, value)
