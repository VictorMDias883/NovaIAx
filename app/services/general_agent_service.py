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

from app.cache.conversation_cache import ConversationCache
from app.clients.ai_client import AIClient
from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, PlanTier
from app.commands.general_agent_command import GeneralAgentCommand
from app.core.logging import get_logger
from app.models.roadmap_day import RoadmapDayStatus
from app.repositories.objective_repository import ObjectiveRepository
from app.repositories.roadmap_day_repository import RoadmapDayRepository
from app.services.agent_prompt_service import (
    build_personalized_system_prompt,
    load_personalization_context,
    parse_personalization_block,
)
from app.services.memory_learning_service import MemoryLearningService

logger = get_logger(__name__)

#: How many objectives are bundled into the context at most.
OBJECTIVES_CONTEXT_LIMIT = 100


class GeneralAgentService:
    """Coordinate the general-agent conversation with user data context."""

    #: Logical agent name used to namespace the conversation cache.
    AGENT_KEY = "general_agent"

    def __init__(
        self,
        session: AsyncSession,
        ai_client: AIClient | AIRouter,
        conversation_cache: ConversationCache | None = None,
    ) -> None:
        """Initialise the service with a database session and an AI client.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
            ai_client: The :class:`AIClient` implementation or the shared
                :class:`AIRouter` used to reach the AI providers.
            conversation_cache: The :class:`ConversationCache` used to keep
                conversation history between turns.  If omitted, a new one
                is created.
        """
        self.session = session
        self.ai_client = ai_client
        self.conversation_cache = conversation_cache or ConversationCache()
        self.objective_repo = ObjectiveRepository(session)
        self.day_repo = RoadmapDayRepository(session)

    async def create_completion(
        self,
        command: GeneralAgentCommand,
        user_id: int,
        plan_tier: PlanTier = PlanTier.FREE,
    ) -> dict[str, object]:
        """Handle one turn of the general-agent workflow.

        Steps:
            1. Load the user's objectives and their roadmap days.
            2. Build a context block summarising objectives and which
               roadmap days were fulfilled.
            3. Personalise the system prompt with the user's agent profile
               when one exists (name/tone, active groups, active sub-agents
               and enabled memories).
            4. Load the previous conversation from the cache and ask the
               AI provider to answer the user's message using that history
               plus the fresh context block.
            5. Parse the ``[personalization]`` marker out of the reply and
               auto-learn explicit statements when learning is enabled.
            6. Store the new user/assistant pair so the next turn keeps
               continuity and memory.

        Args:
            command: The user's message wrapped in a command object.
            user_id: ID of the authenticated user (real database user).

        Returns:
            A dictionary with the assistant's ``assistant_message`` plus the
            ``why`` (reasons) and ``sources`` lists from the marker.
        """
        objectives = await self._load_objective_context(user_id)
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
        ctx = await load_personalization_context(self.session, user_id)
        if ctx is not None:
            system_prompt = build_personalized_system_prompt(system_prompt, ctx)

        messages = await self.conversation_cache.get_messages(self.AGENT_KEY, user_id)
        current_user_message = f"{command.user_message}\n\n{context}"
        to_send = messages + [{"role": "user", "content": current_user_message}]

        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=system_prompt,
                user_message=current_user_message,
                plan_tier=plan_tier,
                user_id=user_id,
                history=to_send,
            ),
        )
        assistant_message, why, sources = parse_personalization_block(assistant.text)

        # The user message is stored without the dynamic context block, which
        # is rebuilt from the database on every turn.
        to_store = messages + [
            {"role": "user", "content": command.user_message},
            {"role": "assistant", "content": assistant_message},
        ]
        await self.conversation_cache.save(self.AGENT_KEY, user_id, to_store)

        try:
            await MemoryLearningService(self.session).learn_from_statement(user_id, command.user_message)
        except Exception:  # pragma: no cover - defensive; a turn must not fail on learning
            logger.exception("Auto-learning failed for user %s", user_id)

        return {"assistant_message": assistant_message, "why": why, "sources": sources}

    async def _load_objective_context(self, user_id: int) -> list:
        """Load at most :data:`OBJECTIVES_CONTEXT_LIMIT` objectives, flagging overflow.

        One extra row is fetched to find out whether the limit actually cut
        anything off.  Dropping the surplus silently is misleading: the
        assistant would state with confidence that a user has no goals,
        while in truth they had more than the context budget allows — and the
        user has no way to tell the difference between "you have no other
        goals" and "I only looked at the first hundred".

        Args:
            user_id: ID of the authenticated user.

        Returns:
            The objectives to include in the context.  When the list was
            truncated it has one entry less than the query returned and a
            warning has been logged.
        """
        fetched = await self.objective_repo.list_by_user(user_id, 0, OBJECTIVES_CONTEXT_LIMIT + 1)
        if len(fetched) <= OBJECTIVES_CONTEXT_LIMIT:
            return fetched

        logger.warning(
            "General agent context truncated for user %s: showing %s of at least %s objectives. "
            "The assistant cannot see the remaining objectives.",
            user_id,
            OBJECTIVES_CONTEXT_LIMIT,
            len(fetched),
        )
        return fetched[:OBJECTIVES_CONTEXT_LIMIT]

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

        # Every objective's days are fetched in one query and grouped here.
        # Querying them per objective made each assistant turn issue one extra
        # round trip per objective, so the latency of replying to "como estão
        # meus objetivos?" grew with the number of objectives while the user
        # waited on an AI provider that takes seconds anyway.
        days_by_objective = await self.day_repo.list_by_objectives([objective.id for objective in objectives])

        for index, objective in enumerate(objectives, start=1):
            lines.append(f"{index}. Título: {objective.title}")
            lines.append(f"   Descrição: {objective.description or 'Não informada'}")
            lines.append(f"   Prazo: {objective.due_date.date().isoformat()}")
            lines.append(f"   Criado em: {objective.created_at.date().isoformat()}")
            if objective.roadmap_updated_at:
                lines.append(f"   Roadmap atualizado em: {objective.roadmap_updated_at.date().isoformat()}")

            days = days_by_objective.get(objective.id, [])
            if days:
                completed = sum(1 for day in days if day.status == RoadmapDayStatus.COMPLETED)
                lines.append(
                    f"   Dias do roadmap ({len(days)} no total, {completed} cumprido(s)):"
                )
                for day in days:
                    date_label = day.day_date.date().isoformat()
                    content = f" — {day.content}" if day.content else ""
                    lines.append(
                        f"     - Dia {day.day_number} ({date_label}): {day.status.value}{content}"
                    )
            else:
                lines.append("   Dias do roadmap: nenhum registrado ainda.")

        return "\n".join(lines)
