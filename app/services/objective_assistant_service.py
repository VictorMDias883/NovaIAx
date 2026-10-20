"""
Service for the specialized objective-creation assistant.

This flow is intentionally separate from the generic chat completion
flow. It uses a dedicated system prompt and a direct AI call to collect
objective data from the user until it can generate a structured JSON
payload. Once the payload is valid, it registers the objective using the
existing objective service.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.conversation_cache import ConversationCache
from app.clients.ai_client import AIClient
from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, PlanTier
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.core.logging import get_logger
from app.utils.json_extraction import extract_json

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.services.objective_service import ObjectiveService


class ObjectiveAssistantService:
    """Coordinate the objective-assistant conversation and persistence flow."""

    #: Logical agent name used to namespace the conversation cache.
    AGENT_KEY = "objective_assistant"

    #: Replied instead of the model's raw JSON when the draft's due date is
    #: not a ``YYYY-MM-DD`` date.  Registration is skipped so the user can
    #: confirm the date on the next turn.
    DUE_DATE_CONFIRMATION_MESSAGE = (
        "Não consegui interpretar a data que você informou, então ainda não criei a sua meta. "
        "Pode me confirmar a data de conclusão no formato YYYY-MM-DD (por exemplo, 2026-12-31)? "
        "Assim que você confirmar, eu concluo o cadastro com o título e a descrição que já coletamos."
    )

    def __init__(
        self,
        session: AsyncSession,
        ai_client: AIClient | AIRouter,
        objective_service: "ObjectiveService | None" = None,
        conversation_cache: ConversationCache | None = None,
    ) -> None:
        self.session = session
        self.ai_client = ai_client
        self.conversation_cache = conversation_cache or ConversationCache()
        if objective_service is None:
            from app.services.objective_service import ObjectiveService

            self.objective_service = ObjectiveService(session, ai_client=self.ai_client)
        else:
            self.objective_service = objective_service

    async def create_completion(
        self,
        command: ObjectiveAssistantCommand,
        user_id: int,
        *,
        plan_tier: PlanTier = PlanTier.FREE,
    ) -> dict[str, object]:
        """Handle one turn of the objective assistant workflow.

        The previous conversation is loaded from the cache and sent to the
        AI together with the current message, preserving context.  When the
        AI provides a complete objective payload that is actually
        registered, the whole cached conversation is deleted so the next
        conversation starts fresh.

        Returns:
            ``{"assistant_message": str, "created_objective": dict | None}``,
            where ``created_objective`` holds the registered objective when
            this turn auto-registered one.
        """
        system_prompt = (
            "Você é um assistente especializado em ajudar o usuário a criar uma meta/objetivo. "
            "Converse naturalmente, faça perguntas uma por vez, colete as informações necessárias "
            "(título, descrição, prazo, motivo e detalhes relevantes) e, quando estiver pronto, "
            "responda EXCLUSIVAMENTE em JSON no formato {\"titulo\": \"string\", \"descricao\": \"string\", \"prazo\": \"YYYY-MM-DD\"}. "
            "Enquanto não tiver todas as informações obrigatórias, responda em texto natural."
        )

        messages = await self.conversation_cache.get_messages(self.AGENT_KEY, user_id)
        to_send = [*messages, {"role": "user", "content": command.user_message}]

        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=system_prompt,
                user_message=command.user_message,
                user_id=user_id,
                history=to_send,
                plan_tier=plan_tier,
            ),
        )
        assistant_message = assistant.text

        # If the AI response contains valid JSON matching the objective schema,
        # we treat it as a completed objective draft and persist it.
        objective_created = False
        created_objective: dict[str, object] | None = None
        parsed = extract_json(assistant_message, context="Objective assistant")
        if parsed is not None and self._is_complete_payload(parsed):
            try:
                # Support both Portuguese and English key names
                if "titulo" in parsed:
                    title = str(parsed["titulo"]).strip()
                    description = str(parsed["descricao"]).strip()
                    raw_due_date = parsed["prazo"]
                else:
                    title = str(parsed["title"]).strip()
                    description = str(parsed["description"]).strip()
                    raw_due_date = parsed["due_date"]
            except (KeyError, TypeError, ValueError):
                title = description = ""
                raw_due_date = None
            else:
                if title and description:
                    due_date = self._parse_due_date(raw_due_date)
                    if due_date is None:
                        # The draft is complete but its deadline is unusable.
                        # Do not register anything and do not echo the raw JSON
                        # back as if the objective had been handled: ask the
                        # user to confirm the date and keep the conversation so
                        # the next turn can finish the job.
                        logger.warning(
                            "Objective assistant returned an unparsable due date; "
                            "asking the user to confirm the date instead of registering",
                            extra={
                                "raw_due_date": str(raw_due_date)[:100],
                                "user_id": user_id,
                            },
                        )
                        assistant_message = self.DUE_DATE_CONFIRMATION_MESSAGE
                    else:
                        command_obj = RegisterObjectiveCommand(
                            title=title, description=description, due_date=due_date
                        )
                        try:
                            result = await self.objective_service.register(
                                command_obj, user_id=user_id, plan_tier=plan_tier
                            )
                        except HTTPException:
                            # Registration failed (e.g. past due date).  Keep the
                            # conversation so the user can continue or retry.
                            await self.conversation_cache.save(
                                self.AGENT_KEY,
                                user_id,
                                [*to_send, {"role": "assistant", "content": assistant_message}],
                            )
                            raise
                        objective_created = True
                        created_objective = result

        if objective_created:
            # Once the objective is actually created, drop the whole cached
            # conversation so a new objective starts a fresh chat.
            await self.conversation_cache.clear(self.AGENT_KEY, user_id)
        else:
            await self.conversation_cache.save(
                self.AGENT_KEY,
                user_id,
                [*to_send, {"role": "assistant", "content": assistant_message}],
            )

        return {"assistant_message": assistant_message, "created_objective": created_objective}

    def _is_complete_payload(self, payload: Any) -> bool:
        """Validate the AI payload structure before it is treated as a completion."""
        if not isinstance(payload, dict):
            return False
        # Accept either Portuguese keys or English equivalents.
        required_sets = [
            {"titulo", "descricao", "prazo"},
            {"title", "description", "due_date"},
        ]
        if not any(req.issubset(payload.keys()) for req in required_sets):
            return False
        # pick keys that exist and ensure they're non-empty strings
        keys = ("titulo", "descricao", "prazo") if "titulo" in payload else ("title", "description", "due_date")
        return all(isinstance(payload[key], str) and bool(str(payload[key]).strip()) for key in keys)

    @staticmethod
    def _parse_due_date(value: object) -> datetime | None:
        """Parse the AI-supplied due date into a timezone-aware datetime.

        Returns ``None`` — rather than raising — when the model hands back
        something that is not a ``YYYY-MM-DD`` date (a prose deadline such as
        "next month", a locale-formatted date, an int, ...), so the caller can
        ask the user to confirm it instead of dropping the whole draft.
        """
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)
