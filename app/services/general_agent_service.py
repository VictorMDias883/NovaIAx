"""
Service for the general agent.

The general agent is a conversational assistant that has access to the
authenticated user's objectives and the days of each roadmap window that
the user has fulfilled. It gathers that data from the database, bundles
it into a context block appended to the user's message, and lets the AI
provider answer based on it.

This flow is intentionally separate from the objective-assistant (which
creates goals) and the generic chat completion (which is bound to an
administrator-managed system prompt).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient
from app.commands.general_agent_command import GeneralAgentCommand
from app.models.roadmap_day import RoadmapDayStatus
from app.repositories.objective_repository import ObjectiveRepository
from app.repositories.roadmap_day_repository import RoadmapDayRepository

#: How many objectives are bundled into the context at most.
OBJECTIVES_CONTEXT_LIMIT = 100


class GeneralAgentService:
    """Coordinate the general-agent conversation with user data context."""

    def __init__(self, session: AsyncSession, ai_client: AIClient) -> None:
        """Initialise the service with a database session and an AI client.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
            ai_client: The :class:`AIClient` implementation used to reach
                the AI provider.
        """
        self.session = session
        self.ai_client = ai_client
        self.objective_repo = ObjectiveRepository(session)
        self.day_repo = RoadmapDayRepository(session)

    async def create_completion(self, command: GeneralAgentCommand, user_id: int) -> dict[str, str]:
        """Handle one turn of the general-agent workflow.

        Steps:
            1. Load the user's objectives and their roadmap days.
            2. Build a context block summarising objectives and which
               roadmap days were fulfilled.
            3. Ask the AI provider to answer the user's message using
               that context.

        Args:
            command: The user's message wrapped in a command object.
            user_id: ID of the authenticated user.

        Returns:
            A dictionary with the assistant's ``assistant_message``.
        """
        objectives = await self.objective_repo.list_by_user(user_id, 0, OBJECTIVES_CONTEXT_LIMIT)
        context = await self._build_context(objectives)

        system_prompt = (
            "Você é o Agente Geral da NovaIAx, um assistente que acompanha o "
            "progresso dos objetivos do usuário. Você tem acesso aos objetivos "
            "cadastrados e aos dias do roadmap de cada objetivo que o usuário "
            "já cumpriu. "
            "Responda em português do Brasil, de forma clara, amigável e objetiva, "
            "baseando-se APENAS nos dados fornecidos no bloco de contexto. "
            "Se o usuário perguntar algo que esteja fora dos dados disponíveis "
            "(ex.: metas não cadastradas ou dias ainda não registrados), "
            "diga educadamente que não possui essa informação. "
            "Não invente objetivos, prazos ou cumprimentos."
        )
        user_message = f"{command.user_message}\n\n{context}"

        assistant_message = await self.ai_client.create_chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
        )
        return {"assistant_message": assistant_message}

    async def _build_context(self, objectives: list) -> str:
        """Build a textual context block describing the user's objectives.

        For each objective the block includes the title, description,
        due date, roadmap dates and the per-day status, so the assistant
        can report which days of the roadmap have been fulfilled.

        Args:
            objectives: The user's :class:`Objective` instances.

        Returns:
            A plain-text context block.
        """
        lines = ["OBJETIVOS DO USUÁRIO (contexto):"]
        if not objectives:
            lines.append("- Nenhum objetivo cadastrado até o momento.")
            return "\n".join(lines)

        for index, objective in enumerate(objectives, start=1):
            lines.append(f"{index}. Título: {objective.title}")
            lines.append(f"   Descrição: {objective.description or 'Não informada'}")
            lines.append(f"   Prazo: {objective.due_date.date().isoformat()}")
            lines.append(f"   Criado em: {objective.created_at.date().isoformat()}")
            if objective.roadmap_updated_at:
                lines.append(f"   Roadmap atualizado em: {objective.roadmap_updated_at.date().isoformat()}")

            days = await self.day_repo.list_by_objective(objective.id)
            if days:
                completed = sum(1 for day in days if day.status == RoadmapDayStatus.COMPLETED)
                lines.append(
                    f"   Dias do roadmap ({len(days)} no total, {completed} cumprido(s)):"
                )
                for day in days:
                    date_label = day.day_date.date().isoformat()
                    lines.append(f"     - Dia {day.day_number} ({date_label}): {day.status.value}")
            else:
                lines.append("   Dias do roadmap: nenhum registrado ainda.")

        return "\n".join(lines)
