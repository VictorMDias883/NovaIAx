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
        commit: bool = True,
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
            commit: Whether to commit immediately.  Pass ``False`` to let the
                caller's transaction own the commit, so the objective and its
                roadmap days are written together (all-or-nothing).

        Returns:
            The newly created :class:`Objective` instance with ``id`` and
            ``created_at`` populated (both are available from the flush).
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
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return objective

    async def get_by_id(self, objective_id: int, for_update: bool = False) -> Objective | None:
        """Return an objective by its ID, or ``None`` if it does not exist.

        Args:
            objective_id: The objective's primary key.
            for_update: Lock the row for the rest of the transaction.  Used
                by the renewal flow, where two concurrent requests could both
                pass the "is the 7-day window over?" check and each write a
                roadmap — the second one would silently overwrite the first.
        """
        stmt = select(Objective).where(Objective.id == objective_id)
        if for_update:
            stmt = stmt.with_for_update()
            # ``populate_existing`` is what makes this re-read meaningful.  The
            # row was already loaded earlier in the request (to check the
            # window before calling the AI), so it sits in the identity map and
            # SQLAlchemy would otherwise hand back the *cached* instance with its
            # original ``roadmap_updated_at`` — silently re-checking a stale
            # timestamp and defeating the very race this lock exists to stop.
            stmt = stmt.execution_options(populate_existing=True)
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
        return list(result.scalars().all())

    async def update(
        self,
        objective_id: int,
        commit: bool = True,
        *,
        for_update: bool = False,
        objective: Objective | None = None,
        **values,
    ) -> Objective | None:
        """Update an objective's fields and return the updated instance.

        Args:
            objective_id: The objective to update.
            commit: Whether to commit immediately.  ``False`` keeps the
                change pending in the caller's transaction (used by the
                roadmap renewal, which must also insert days).
            for_update: Lock the row before updating, so concurrent renewals
                of the same objective serialize instead of racing.
            objective: An already-loaded (and, for a renewal, already
                locked) instance to update in place.  Passing it skips the
                extra ``SELECT`` — and, importantly, avoids re-reading a row
                whose lock the caller is already holding.
            **values: The columns to change (e.g. ``title``, ``description``,
                ``due_date``, ``roadmap``, ``roadmap_updated_at``).

        Returns:
            The updated :class:`Objective`, or ``None`` if it does not exist.
        """
        if objective is None:
            objective = await self.get_by_id(objective_id, for_update=for_update)
        if objective is None:
            return None
        for key, value in values.items():
            setattr(objective, key, value)
        if commit:
            await self.session.commit()
            # No ``refresh`` is needed: the instance was loaded through this
            # session and the factory uses ``expire_on_commit=False``, so the
            # updated values are already in memory.  Re-reading would only add
            # a ``SELECT`` per update.
        return objective
