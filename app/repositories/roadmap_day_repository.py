"""
Repository layer for the ``RoadmapDay`` model.

Follows the same repository pattern as :mod:`app.repositories.objective_repository`
— isolating SQLAlchemy queries behind a simple, testable interface.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus


class RoadmapDayRepository:
    """Data-access layer for :class:`RoadmapDay` entities."""

    def __init__(self, session: AsyncSession) -> None:
        """Store the async database session for use in queries.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session

    async def create(
        self,
        *,
        objective_id: int,
        day_number: int,
        day_date,
        status: RoadmapDayStatus = RoadmapDayStatus.PENDING,
        content: str | None = None,
    ) -> RoadmapDay:
        """Create and persist a single roadmap day.

        Args:
            objective_id: ID of the owning objective.
            day_number: Position of the day inside its window (1..N).
            day_date: Calendar date the day refers to.
            status: Initial status of the day.
            content: Optional basic meta / minimum objective for the day.

        Returns:
            The newly created :class:`RoadmapDay` instance.
        """
        day = RoadmapDay(
            objective_id=objective_id,
            day_number=day_number,
            day_date=day_date,
            status=status,
            content=content,
        )
        self.session.add(day)
        await self.session.commit()
        await self.session.refresh(day)
        return day

    async def create_many(self, days: list[RoadmapDay]) -> list[RoadmapDay]:
        """Persist a batch of roadmap days in a single transaction.

        Args:
            days: A list of unsaved :class:`RoadmapDay` instances.

        Returns:
            The same instances with IDs populated.
        """
        self.session.add_all(days)
        await self.session.commit()
        for day in days:
            await self.session.refresh(day)
        return days

    async def get_by_id(self, day_id: int) -> RoadmapDay | None:
        """Return a roadmap day by its ID, or ``None`` if it does not exist."""
        stmt = select(RoadmapDay).where(RoadmapDay.id == day_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_by_objective(self, objective_id: int) -> list[RoadmapDay]:
        """Return all roadmap days of an objective in calendar order.

        ``day_number`` restarts at 1 on every 7-day renewal window, so it
        alone cannot order days across windows — ordering must be by
        ``day_date`` first (with ``day_number`` as a tiebreaker).
        """
        stmt = (
            select(RoadmapDay)
            .where(RoadmapDay.objective_id == objective_id)
            .order_by(RoadmapDay.day_date, RoadmapDay.day_number)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update_status(
        self,
        day_id: int,
        status: RoadmapDayStatus,
        completed_at=None,
    ) -> RoadmapDay | None:
        """Update a roadmap day's status and completion timestamp.

        Args:
            day_id: ID of the roadmap day to update.
            status: The new :class:`RoadmapDayStatus`.
            completed_at: Timestamp set when the day is marked as
                completed (``None`` otherwise).

        Returns:
            The updated :class:`RoadmapDay`, or ``None`` if the day
            does not exist.
        """
        day = await self.get_by_id(day_id)
        if day is None:
            return None
        day.status = status
        day.completed_at = completed_at
        await self.session.commit()
        await self.session.refresh(day)
        return day
