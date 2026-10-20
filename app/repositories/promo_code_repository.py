"""
Repository layer for :class:`~app.models.promo_code.PromoCode`.

A thin data-access wrapper: the service owns transactions and business rules,
the repository only builds queries and stages writes.  :meth:`get_for_update`
is the lock primitive the redemption flow relies on to serialise concurrent
redemptions of the same code.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promo_code import PromoCode


class PromoCodeRepository:
    """Data-access layer for promo codes."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, code: str) -> PromoCode | None:
        """Return the code row matching a normalised code, if any."""
        result = await self.session.execute(select(PromoCode).where(PromoCode.code == code))
        return result.scalar_one_or_none()

    async def get_by_id(self, code_id: int) -> PromoCode | None:
        """Return the code with ``code_id``, if any."""
        return await self.session.get(PromoCode, code_id)

    async def get_for_update(self, code: str) -> PromoCode | None:
        """Lock and return the code row for the duration of the transaction.

        ``SELECT ... FOR UPDATE`` serialises concurrent redemptions of the same
        code so the volume ceiling can be checked and the redemption row
        inserted atomically.  SQLite (tests) ignores the clause but still
        serialises writers.
        """
        result = await self.session.execute(
            select(PromoCode).where(PromoCode.code == code).with_for_update()
        )
        return result.scalar_one_or_none()

    async def list(
        self, *, include_inactive: bool = True, limit: int = 100, offset: int = 0
    ) -> list[PromoCode]:
        """Return codes ordered newest first."""
        stmt = select(PromoCode)
        if not include_inactive:
            stmt = stmt.where(PromoCode.active.is_(True))
        stmt = stmt.order_by(PromoCode.id.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self, *, include_inactive: bool = True) -> int:
        """Return the total number of codes."""
        stmt = select(func.count()).select_from(PromoCode)
        if not include_inactive:
            stmt = stmt.where(PromoCode.active.is_(True))
        return int((await self.session.execute(stmt)).scalar_one())

    def add(self, code: PromoCode) -> None:
        """Stage a new code for insertion."""
        self.session.add(code)

    def update_fields(self, code: PromoCode, values: dict[str, Any]) -> None:
        """Apply ``values`` to ``code`` in the current session."""
        for key, value in values.items():
            setattr(code, key, value)
