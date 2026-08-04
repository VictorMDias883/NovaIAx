"""
Repository layer for the ``Objective`` model.

Follows the same repository pattern as
:mod:`app.repositories.user_repository` — isolating SQLAlchemy queries
behind a simple, testable interface.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.objective import Objective


class ObjectiveRepository:
    """Data-access layer for :class:`Objective` entities.

    Currently exposes only a ``create`` method, but additional methods
    (e.g. ``get_by_user``, ``update``, ``delete``) would follow the
    same pattern.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the async database session for use in queries.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session

    async def create(self, *, title: str, description: str | None, due_date, user_id: int) -> Objective:
        """Create and persist a new objective.

        The ``*`` enforces keyword-only arguments for clarity.

        Args:
            title: Short title for the objective.
            description: Optional longer description.
            due_date: Deadline for the objective (timezone-aware datetime).
            user_id: ID of the owning user (foreign key to ``users.id``).

        Returns:
            The newly created :class:`Objective` instance (with ``id``
            populated after ``commit`` and ``refresh``).
        """
        objective = Objective(title=title, description=description, due_date=due_date, user_id=user_id)
        self.session.add(objective)
        await self.session.commit()
        # ``refresh`` reloads the instance from the database so that
        # server-generated fields (e.g. ``id``, ``created_at``) are
        # available on the returned object.
        await self.session.refresh(objective)
        return objective
