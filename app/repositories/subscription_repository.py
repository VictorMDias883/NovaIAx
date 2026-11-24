"""
Repository layer for the billing models (``Subscription`` / ``PurchaseEvent``).

The repository abstracts the SQLAlchemy queries so the billing service stays
free of ORM details.  Writes are staged with ``add``/``flush`` and left for
the service (which owns the ``session.begin()`` transaction) to commit,
matching the pattern used elsewhere in :mod:`app.repositories`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.purchase_event import PurchaseEvent
from app.models.subscription import Subscription


class SubscriptionRepository:
    """Data-access layer for :class:`Subscription` and :class:`PurchaseEvent`."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async database session for use in queries."""
        self.session = session

    async def get_by_user(self, user_id: int) -> Subscription | None:
        """Return the subscription owned by ``user_id`` (``None`` if any)."""
        result = await self.session.execute(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_token_hash(self, token_hash: str) -> Subscription | None:
        """Return the subscription matching a purchase-token hash.

        Used by the RTDN handler to find the owner of a notification without
        the raw token ever leaving Google's report.
        """
        result = await self.session.execute(
            select(Subscription).where(Subscription.purchase_token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def token_owned_by_other_user(self, token_hash: str, user_id: int) -> bool:
        """Return whether another account already holds this purchase token.

        A Play purchase token belongs to exactly one NovaIAx account; letting a
        second account verify it would hand one paid subscription to many users.
        """
        result = await self.session.execute(
            select(Subscription.id)
            .where(Subscription.purchase_token_hash == token_hash)
            .where(Subscription.user_id != user_id)
            .limit(1)
        )
        return result.first() is not None

    def add(self, subscription: Subscription) -> None:
        """Stage a new subscription for insertion."""
        self.session.add(subscription)

    def add_event(self, event: PurchaseEvent) -> None:
        """Stage a new audit event for insertion."""
        self.session.add(event)
