"""
Repository layer for the ``Objective`` model.

Follows the same repository pattern as
:mod:`app.repositories.user_repository` — isolating SQLAlchemy queries
behind a simple, testable interface.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.objective import Objective


class ObjectiveRepository:
    """Data-access layer for :class:`Objective` entities.

    Isolates SQLAlchemy queries behind a simple, testable interface.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the async database session for use in queries.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session

    async def create(
        self,
        *,
        title: str,
        description: str | None,
        due_date,
        user_id: int,
        roadmap: str | None = None,
        roadmap_updated_at=None,
    ) -> Objective:
        """Create and persist a new objective.

        The ``*`` enforces keyword-only arguments for clarity.

        Args:
            title: Short title for the objective.
            description: Optional longer description.
            due_date: Deadline for the objective (timezone-aware datetime).
            user_id: ID of the owning user (foreign key to ``users.id``).
            roadmap: Optional AI-generated roadmap (markdown text).
            roadmap_updated_at: Optional timestamp of the roadmap
                generation (used to enforce the renewal window).

        Returns:
            The newly created :class:`Objective` instance (with ``id``
            populated after ``commit`` and ``refresh``).
        """
        objective = Objective(
            title=title,
            description=description,
            due_date=due_date,
            user_id=user_id,
            roadmap=roadmap,
            roadmap_updated_at=roadmap_updated_at,
        )
        self.session.add(objective)
        await self.session.commit()
        # ``refresh`` reloads the instance from the database so that
        # server-generated fields (e.g. ``id``, ``created_at``) are
        # available on the returned object.
        await self.session.refresh(objective)
        return objective

    async def get_by_id(self, objective_id: int) -> Objective | None:
        """Return an objective by its ID, or ``None`` if it does not exist."""
        stmt = select(Objective).where(Objective.id == objective_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_by_user(self, user_id: int, offset: int = 0, limit: int = 50) -> list[Objective]:
        """Return a user's objectives ordered by ID, with pagination."""
        stmt = (
            select(Objective)
            .where(Objective.user_id == user_id)
            .order_by(Objective.id)
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update(self, objective_id: int, **values) -> Objective | None:
        """Update an objective's fields and return the updated instance.

        Accepts keyword arguments for the columns to change (e.g.
        ``title``, ``description``, ``due_date``, ``roadmap``).  Returns
        ``None`` if the objective does not exist.
        """
        objective = await self.get_by_id(objective_id)
        if objective is None:
            return None
        for key, value in values.items():
            setattr(objective, key, value)
        await self.session.commit()
        await self.session.refresh(objective)
        return objective
