"""
Service layer for roadmap-day management.

This service handles the business logic for tracking individual days of
an objective's roadmap: creating the days whenever a roadmap window is
generated, listing them for the owner, and updating their status (e.g.
marking a day as fulfilled).

    Router → Command → Service → Repository → Database
"""

from datetime import UTC, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus
from app.repositories.objective_repository import ObjectiveRepository
from app.repositories.roadmap_day_repository import RoadmapDayRepository


class RoadmapDayService:
    """Service for creating, listing, and updating roadmap days."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialise the service with a database session.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session
        self.day_repo = RoadmapDayRepository(session)
        self.objective_repo = ObjectiveRepository(session)

    async def create_days(
        self,
        objective_id: int,
        period_start: datetime,
        period_end: datetime,
        contents: dict[int, str] | None = None,
    ) -> list[RoadmapDay]:
        """Create one roadmap day per calendar date in the given window.

        The window is inclusive on both ends: if ``period_start`` and
        ``period_end`` fall on the same calendar date, a single day is
        created.  When ``contents`` is provided it maps each ``day_number``
        to the day's basic meta / minimum objective.

        Args:
            objective_id: ID of the owning objective.
            period_start: Start of the window (timezone-aware).
            period_end: End of the window (timezone-aware).
            contents: Optional mapping of ``day_number`` → day meta.

        Returns:
            The list of created :class:`RoadmapDay` instances.
        """
        start = self._ensure_aware(period_start)
        end = self._ensure_aware(period_end)
        if end.date() < start.date():
            raise HTTPException(status_code=400, detail="period_end cannot precede period_start")

        start_date = start.date()
        end_date = end.date()
        total = (end_date - start_date).days + 1

        # Load existing days for the objective and index them by calendar date
        # so we can return existing records for the requested window instead
        # of silently skipping them. This ensures renewals return the proper
        # list of days even when they were created previously.
        existing = await self.day_repo.list_by_objective(objective_id)
        existing_map: dict = {d.day_date.date(): d for d in existing}

        # Prepare lists: `to_create` holds new RoadmapDay instances to persist,
        # `result_days` will accumulate either existing or newly created days
        # in window order.
        to_create: list[RoadmapDay] = []
        result_days: list[RoadmapDay] = []
        existing_to_refresh: list[RoadmapDay] = []

        for day_number in range(1, total + 1):
            day_date = datetime.combine(start_date + timedelta(days=day_number - 1), time.min, tzinfo=UTC)
            content = contents.get(day_number) if contents else None
            if day_date.date() in existing_map:
                # Use the existing persisted day for this calendar date.
                existing_day = existing_map[day_date.date()]
                # A renewal may regenerate a window whose calendar dates
                # already have days (e.g. clock skew or an early renewal).
                # Apply the fresh meta so day records stay in sync with the
                # new roadmap instead of silently keeping the old content.
                if content and content != existing_day.content:
                    existing_day.content = content
                    existing_to_refresh.append(existing_day)
                result_days.append(existing_day)
            else:
                # Create an unsaved RoadmapDay instance to be persisted.
                to_create.append(
                    RoadmapDay(
                        objective_id=objective_id,
                        day_number=day_number,
                        day_date=day_date,
                        status=RoadmapDayStatus.PENDING,
                        content=content,
                    )
                )

        if existing_to_refresh:
            await self.session.commit()

        # Persist any newly created days and refresh them to obtain IDs.
        created: list[RoadmapDay] = []
        if to_create:
            created = await self.day_repo.create_many(to_create)

        # Merge existing and created days preserving window order.
        # Build a mapping for created days by date for quick lookup.
        created_map = {d.day_date.date(): d for d in created}

        merged: list[RoadmapDay] = []
        for day_number in range(1, total + 1):
            d_date = (start_date + timedelta(days=day_number - 1))
            if d_date in existing_map:
                merged.append(existing_map[d_date])
            elif d_date in created_map:
                merged.append(created_map[d_date])

        return merged

    async def list_days(self, objective_id: int, user_id: int, role: str) -> list[RoadmapDay]:
        """Return an objective's roadmap days (owner or admin only).

        Args:
            objective_id: ID of the objective.
            user_id: ID of the requesting user.
            role: The requesting user's role (``"ADMIN"`` can access any
                objective).

        Returns:
            The objective's roadmap days ordered by day number.

        Raises:
            HTTPException(404): If the objective does not exist.
            HTTPException(403): If the user does not own the objective
                and is not an administrator.
        """
        objective = await self.objective_repo.get_by_id(objective_id)
        if objective is None:
            raise HTTPException(status_code=404, detail="Objective not found")
        if str(user_id) != str(objective.user_id) and role != "ADMIN":
            raise HTTPException(status_code=403, detail="Not authorized")
        return await self.day_repo.list_by_objective(objective_id)

    async def set_day_status(
        self,
        objective_id: int,
        day_id: int,
        user_id: int,
        role: str,
        status: RoadmapDayStatus,
    ) -> RoadmapDay:
        """Update the status of a single roadmap day.

        When the day is marked as completed, ``completed_at`` is set to
        the current time; otherwise it is cleared.

        Args:
            objective_id: ID of the objective owning the day.
            day_id: ID of the roadmap day to update.
            user_id: ID of the requesting user.
            role: The requesting user's role (``"ADMIN"`` can update any
                objective).
            status: The new status.

        Returns:
            The updated :class:`RoadmapDay` instance.

        Raises:
            HTTPException(404): If the objective or the day does not exist.
            HTTPException(403): If the user does not own the objective
                and is not an administrator.
        """
        objective = await self.objective_repo.get_by_id(objective_id)
        if objective is None:
            raise HTTPException(status_code=404, detail="Objective not found")
        if str(user_id) != str(objective.user_id) and role != "ADMIN":
            raise HTTPException(status_code=403, detail="Not authorized")

        day = await self.day_repo.get_by_id(day_id)
        if day is None or day.objective_id != objective_id:
            raise HTTPException(status_code=404, detail="Roadmap day not found")

        completed_at = datetime.now(UTC) if status == RoadmapDayStatus.COMPLETED else None
        updated = await self.day_repo.update_status(day_id, status=status, completed_at=completed_at)
        return updated

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        """Return the datetime with UTC timezone, assuming UTC for naive values.

        Some drivers (e.g. SQLite) return timezone-naive datetimes even
        for ``DateTime(timezone=True)`` columns, so comparisons against
        ``datetime.now(UTC)`` need this normalisation.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
