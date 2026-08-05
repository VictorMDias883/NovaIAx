"""
Service layer for objective management.

This :class:`ObjectiveService` handles the business logic for creating
objectives (tasks/goals).  It sits between the API router and the
repository layer:

    Router → Command → Service → Repository → Database

Responsibilities:
    - Validate that the objective's ``due_date`` is not in the past.
    - Delegate persistence to :class:`ObjectiveRepository`.
    - Return a plain dictionary representation of the created objective.
"""

from datetime import datetime, UTC

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.register_objective_command import RegisterObjectiveCommand
from app.repositories.objective_repository import ObjectiveRepository


class ObjectiveService:
    """Service for creating and managing objectives.

    Each method receives a :class:`RegisterObjectiveCommand` (or
    similar command object) and a database session, performs
    validation, and delegates to the repository.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialise the service with a database session.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session

    async def register(self, command: RegisterObjectiveCommand, user_id: int) -> dict[str, object]:
        """Create a new objective for the given user.

        Validation:
            - The ``due_date`` must not be in the past.  If it is,
              a 400 Bad Request is raised.

        Steps:
            1. Validate the due date.
            2. Create the objective via the repository.
            3. Return a dictionary with the objective's fields.

        Args:
            command: A :class:`RegisterObjectiveCommand` containing
                the title, description, and due date.
            user_id: The ID of the user who owns this objective.

        Returns:
            A dictionary with ``id``, ``title``, ``description``,
            ``due_date``, and ``user_id`` keys.

        Raises:
            HTTPException(400): If ``due_date`` is in the past.
        """
        if command.due_date < datetime.now(UTC):
            raise HTTPException(status_code=400, detail="due_date cannot be in the past")

        repo = ObjectiveRepository(self.session)
        objective = await repo.create(
            title=command.title,
            description=command.description,
            due_date=command.due_date,
            user_id=user_id,
        )
        return {
            "id": objective.id,
            "title": objective.title,
            "description": objective.description,
            "due_date": objective.due_date,
            "user_id": objective.user_id,
        }
