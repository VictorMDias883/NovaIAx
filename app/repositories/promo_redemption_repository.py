"""
Repository layer for :class:`~app.models.promo_redemption.PromoRedemption`.

This is the entitlement ledger: the resolver asks for the user's still-active
plan grants, and the redemption flow counts rows to enforce volume and
per-user limits.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promo_redemption import PromoRedemption


class PromoRedemptionRepository:
    """Data-access layer for granted entitlements."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_for_code(self, code_id: int) -> int:
        """Total redemptions recorded for a code."""
        stmt = (
            select(func.count())
            .select_from(PromoRedemption)
            .where(PromoRedemption.code_id == code_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_for_user(self, code_id: int, user_id: int) -> int:
        """Redemptions of a code by one user (per-user limit check)."""
        stmt = (
            select(func.count())
            .select_from(PromoRedemption)
            .where(PromoRedemption.code_id == code_id, PromoRedemption.user_id == user_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def active_plan_grants(self, user_id: int, now: datetime) -> list[PromoRedemption]:
        """Return the user's plan grants that have not expired at ``now``."""
        stmt = (
            select(PromoRedemption)
            .where(
                PromoRedemption.user_id == user_id,
                PromoRedemption.plan.is_not(None),
                or_(
                    PromoRedemption.granted_until.is_(None),
                    PromoRedemption.granted_until > now,
                ),
            )
            .order_by(PromoRedemption.granted_until.desc().nullslast())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_user(self, user_id: int) -> list[PromoRedemption]:
        """Return every grant held by a user, newest first."""
        result = await self.session.execute(
            select(PromoRedemption)
            .where(PromoRedemption.user_id == user_id)
            .order_by(PromoRedemption.redeemed_at.desc(), PromoRedemption.id.desc())
        )
        return list(result.scalars().all())

    async def list_for_code(
        self, code_id: int, *, limit: int = 200, offset: int = 0
    ) -> list[PromoRedemption]:
        """Return the redemption ledger for a code, newest first."""
        result = await self.session.execute(
            select(PromoRedemption)
            .where(PromoRedemption.code_id == code_id)
            .order_by(PromoRedemption.redeemed_at.desc(), PromoRedemption.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_source(self, source: str, *, limit: int = 50) -> list[PromoRedemption]:
        """Return grants with a given ``source`` (e.g. manual admin grants)."""
        result = await self.session.execute(
            select(PromoRedemption)
            .where(PromoRedemption.source == source)
            .order_by(PromoRedemption.redeemed_at.desc(), PromoRedemption.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def bonus_totals(self, user_id: int, now: datetime) -> int:
        """Sum of still-active bonus messages granted to a user."""
        stmt = (
            select(func.coalesce(func.sum(PromoRedemption.bonus_messages), 0))
            .where(
                PromoRedemption.user_id == user_id,
                PromoRedemption.bonus_messages.is_not(None),
                or_(
                    PromoRedemption.granted_until.is_(None),
                    PromoRedemption.granted_until > now,
                ),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    def add(self, redemption: PromoRedemption) -> None:
        """Stage a new grant for insertion."""
        self.session.add(redemption)
