"""
Service layer for objective management.

This :class:`ObjectiveService` handles the business logic for creating
and managing objectives (tasks/goals).  It sits between the API router
and the repository layer:

    Router → Command → Service → Repository → Database

Responsibilities:
    - Validate that the objective's ``due_date`` is not in the past.
    - Generate a 7-day roadmap for the objective using the AI provider.
    - Renew the roadmap every 7 days until the objective's due date.
    - Delegate persistence to :class:`ObjectiveRepository`.
    - Return plain dictionary representations of objectives.
"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient, GroqAIClient
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.repositories.objective_repository import ObjectiveRepository
from app.services.roadmap_day_service import RoadmapDayService


class ObjectiveService:
    """Service for creating and managing objectives.

    Each method receives a :class:`RegisterObjectiveCommand` (or
    similar command object) and a database session, performs
    validation, delegates to the repository, and orchestrates the
    AI-generated roadmap.
    """

    #: Length (in days) of each roadmap window.  A roadmap always covers
    #: only this many days ahead and is renewed by the user once it
    #: expires.
    ROADMAP_WINDOW_DAYS = 7

    def __init__(self, session: AsyncSession, ai_client: AIClient | None = None) -> None:
        """Initialise the service with a database session and an AI client.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
            ai_client: Optional AI client used to generate roadmaps.  If
                omitted, a :class:`GroqAIClient` is created lazily.
        """
        self.session = session
        self.ai_client = ai_client

    async def register(self, command: RegisterObjectiveCommand, user_id: int) -> dict[str, object]:
        """Create a new objective for the given user.

        Validation:
            - The ``due_date`` must not be in the past.  If it is,
              a 400 Bad Request is raised.

        Steps:
            1. Validate the due date.
            2. Generate an AI roadmap covering the first 7-day window.
            3. Create the objective (with the roadmap) via the repository.
            4. Return a dictionary with the objective's fields.

        Args:
            command: A :class:`RegisterObjectiveCommand` containing
                the title, description, and due date.
            user_id: The ID of the user who owns this objective.

        Returns:
            A dictionary with ``id``, ``title``, ``description``,
            ``roadmap``, ``roadmap_updated_at``, ``due_date``, and
            ``user_id`` keys.

        Raises:
            HTTPException(400): If ``due_date`` is in the past.
            HTTPException(502): If the AI provider fails to generate
                the roadmap.
        """
        if command.due_date < datetime.now(UTC):
            raise HTTPException(status_code=400, detail="due_date cannot be in the past")

        now = datetime.now(UTC)
        period_start = now
        period_end = min(period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS), command.due_date)
        roadmap = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=None,
        )

        repo = ObjectiveRepository(self.session)
        objective = await repo.create(
            title=command.title,
            description=command.description,
            due_date=command.due_date,
            user_id=user_id,
            roadmap=roadmap,
            roadmap_updated_at=now,
        )
        await self._create_roadmap_days(objective.id, period_start, period_end)
        return self._to_dict(objective)

    async def renew_roadmap(self, objective_id: int, user_id: int, role: str) -> dict[str, object]:
        """Renew an objective's roadmap for the next 7-day window.

        The roadmap can only be renewed once :attr:`ROADMAP_WINDOW_DAYS`
        days have elapsed since the last generation.  The new roadmap
        covers the following 7-day window (capped at the due date) and
        uses the previous roadmap as context for continuity.

        Args:
            objective_id: ID of the objective to renew.
            user_id: ID of the requesting user (used for ownership).
            role: The requesting user's role (``"ADMIN"`` can renew any
                objective).

        Returns:
            A dictionary with the updated objective's fields.

        Raises:
            HTTPException(404): If the objective does not exist.
            HTTPException(403): If the user does not own the objective
                and is not an administrator.
            HTTPException(409): If the renewal window has not elapsed yet.
            HTTPException(502): If the AI provider fails to generate
                the roadmap.
        """
        repo = ObjectiveRepository(self.session)
        objective = await repo.get_by_id(objective_id)
        if objective is None:
            raise HTTPException(status_code=404, detail="Objective not found")

        if str(user_id) != str(objective.user_id) and role != "ADMIN":
            raise HTTPException(status_code=403, detail="Not authorized")

        now = datetime.now(UTC)
        last_update = objective.roadmap_updated_at or objective.created_at
        last_update = self._ensure_aware(last_update)
        if now < last_update + timedelta(days=self.ROADMAP_WINDOW_DAYS):
            next_window = last_update + timedelta(days=self.ROADMAP_WINDOW_DAYS)
            raise HTTPException(
                status_code=409,
                detail=f"Roadmap can only be renewed every {self.ROADMAP_WINDOW_DAYS} days. "
                f"Available after {next_window.isoformat()}",
            )

        if self._ensure_aware(objective.due_date) < now:
            raise HTTPException(status_code=400, detail="Objective already expired")

        period_start = now
        due_date = self._ensure_aware(objective.due_date)
        period_end = min(period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS), due_date)
        command = RegisterObjectiveCommand(
            title=objective.title,
            description=objective.description,
            due_date=objective.due_date,
        )
        roadmap = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=objective.roadmap,
        )

        updated = await repo.update(
            objective_id,
            roadmap=roadmap,
            roadmap_updated_at=now,
        )
        await self._create_roadmap_days(updated.id, period_start, period_end)
        return self._to_dict(updated)

    def _to_dict(self, objective) -> dict[str, object]:
        """Convert an :class:`Objective` instance into a plain dictionary."""
        return {
            "id": objective.id,
            "title": objective.title,
            "description": objective.description,
            "roadmap": objective.roadmap,
            "roadmap_updated_at": objective.roadmap_updated_at,
            "due_date": objective.due_date,
            "user_id": objective.user_id,
        }

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

    async def _create_roadmap_days(
        self,
        objective_id: int,
        period_start: datetime,
        period_end: datetime,
    ) -> None:
        """Create one roadmap-day record per calendar day of the window.

        A roadmap window covers exactly :attr:`ROADMAP_WINDOW_DAYS`
        calendar days, so the day records span from ``period_start`` up
        to ``period_start + ROADMAP_WINDOW_DAYS - 1`` (capped by the
        objective's due date).  Delegates to :class:`RoadmapDayService`
        so that the tracking records stay in sync with the roadmap.
        """
        window_end = min(
            period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS - 1),
            period_end,
        )
        await RoadmapDayService(self.session).create_days(objective_id, period_start, window_end)

    async def _generate_roadmap(
        self,
        command: RegisterObjectiveCommand,
        *,
        period_start: datetime,
        period_end: datetime,
        previous_roadmap: str | None,
    ) -> str:
        """Generate a roadmap for a 7-day window via the AI provider.

        Builds a system prompt instructing the model to produce an
        actionable markdown roadmap for the given period, using the
        previous roadmap as context so the plan stays continuous.

        Args:
            command: The objective's registration data.
            period_start: Start of the period covered by the roadmap.
            period_end: End of the period covered by the roadmap.
            previous_roadmap: The previous roadmap, or ``None`` for the
                first window.

        Returns:
            The AI-generated roadmap as a string.

        Raises:
            HTTPException(502): If the AI provider is unavailable or
                fails to generate a roadmap.
        """
        client = self.ai_client or GroqAIClient()
        system_prompt = (
            "Você é um especialista em planejamento estratégico de metas. "
            "As metas são executadas em janelas de 7 dias: o roadmap sempre "
            "cobre apenas o próximo período de 7 dias e é renovado quando "
            "essa janela termina. "
            "Com base na meta fornecida, crie um roadmap detalhado e acionável "
            "em markdown exclusivamente para o período informado, com etapas e "
            "marcos dentro desse intervalo. Use o roadmap anterior como contexto "
            "para dar continuidade ao plano. "
            "Organize com títulos (##) e listas. Responda apenas com o roadmap."
        )
        user_message = (
            f"Título: {command.title}\n"
            f"Descrição: {command.description or 'Não informada'}\n"
            f"Prazo final: {command.due_date.date().isoformat()}\n"
            f"Período do roadmap: {period_start.date().isoformat()} a {period_end.date().isoformat()}\n"
            f"Roadmap anterior (contexto):\n{previous_roadmap or 'Nenhum'}"
        )
        return await client.create_chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
        )
