"""
Repository layer for the ``RoadmapDay`` model.

Follows the same repository pattern as :mod:`app.repositories.objective_repository`
— isolating SQLAlchemy queries behind a simple, testable interface.
"""

from collections.abc import Sequence

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
        commit: bool = True,
    ) -> RoadmapDay:
        """Create and persist a single roadmap day.

        Args:
            objective_id: ID of the owning objective.
            day_number: Position of the day inside its window (1..N).
            day_date: Calendar date the day refers to.
            status: Initial status of the day.
            content: Optional basic meta / minimum objective for the day.
            commit: Whether to commit immediately.  Pass ``False`` to let an
                outer transaction (e.g. the objective + roadmap-day write in
                :meth:`app.services.objective_service.ObjectiveService.register`)
                own the commit, so both parts land or neither does.

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
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return day

    async def create_many(self, days: list[RoadmapDay], commit: bool = True) -> list[RoadmapDay]:
        """Persist a batch of roadmap days in a single transaction.

        No per-row ``refresh`` is issued.  SQLAlchemy populates the primary
        key of every inserted row during the flush (and the session is built
        with ``expire_on_commit=False``), so re-reading each row back would
        cost one extra ``SELECT`` per day — seven wasted round-trips for every
        roadmap window, on a path that already calls the AI provider.

        Args:
            days: A list of unsaved :class:`RoadmapDay` instances.
            commit: Whether to commit the batch.  ``False`` leaves the rows
                pending inside the caller's transaction.

        Returns:
            The same instances with IDs populated.
        """
        if not days:
            return days
        self.session.add_all(days)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
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
        return list(result.scalars().all())

    async def list_by_objectives(self, objective_ids: Sequence[int]) -> dict[int, list[RoadmapDay]]:
        """Return the roadmap days of several objectives with **one** query.

        Rendering a page of objectives needs the days of every row on the
        page.  Fetching them per objective is the classic N+1: 20 objectives
        become 21 round-trips, and each one costs latency the user waits
        through.  This does a single ``IN (...)`` query and groups the rows
        in Python, so the round-trip count stops growing with the page size.

        Args:
            objective_ids: The objective ids to load days for.  An empty
                sequence performs no query at all.

        Returns:
            ``{objective_id: [day, ...]}`` — days in the same calendar order
            :meth:`list_by_objective` guarantees.  Objectives without any day
            are simply absent from the mapping.
        """
        if not objective_ids:
            return {}

        stmt = (
            select(RoadmapDay)
            .where(RoadmapDay.objective_id.in_(list(dict.fromkeys(objective_ids))))
            .order_by(RoadmapDay.objective_id, RoadmapDay.day_date, RoadmapDay.day_number)
        )
        result = await self.session.execute(stmt)

        grouped: dict[int, list[RoadmapDay]] = {}
        for day in result.scalars().all():
            grouped.setdefault(day.objective_id, []).append(day)
        return grouped

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

    async def delete(self, day_id: int) -> bool:
        """Hard-delete a roadmap day by its ID.

        Args:
            day_id: ID of the roadmap day to remove.

        Returns:
            ``True`` if the day was deleted, ``False`` if no day with
            the given ID exists.
        """
        day = await self.get_by_id(day_id)
        if day is None:
            return False
        await self.session.delete(day)
        await self.session.commit()
        return True
