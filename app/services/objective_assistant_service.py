"""
Service for the specialized objective-creation assistant.

This flow is intentionally separate from the generic chat completion
flow. It uses a dedicated system prompt and a direct AI call to collect
objective data from the user until it can generate a structured JSON
payload. Once the payload is valid, it registers the objective using the
existing objective service.
"""

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.commands.register_objective_command import RegisterObjectiveCommand

if TYPE_CHECKING:
    from app.services.objective_service import ObjectiveService


class ObjectiveAssistantService:
    """Coordinate the objective-assistant conversation and persistence flow."""

    def __init__(self, session: AsyncSession, ai_client: AIClient, objective_service: "ObjectiveService | None" = None) -> None:
        self.session = session
        self.ai_client = ai_client
        self.objective_service = objective_service
        if self.objective_service is None:
            from app.services.objective_service import ObjectiveService

            self.objective_service = ObjectiveService(session, ai_client=self.ai_client)

    async def create_completion(self, command: ObjectiveAssistantCommand, user_id: int) -> dict[str, str]:
        """Handle one turn of the objective assistant workflow."""
        system_prompt = (
            "Você é um assistente especializado em ajudar o usuário a criar uma meta/objetivo. "
            "Converse naturalmente, faça perguntas uma por vez, colete as informações necessárias "
            "(título, descrição, prazo, motivo e detalhes relevantes) e, quando estiver pronto, "
            "responda EXCLUSIVAMENTE em JSON no formato {\"titulo\": \"string\", \"descricao\": \"string\", \"prazo\": \"YYYY-MM-DD\"}. "
            "Enquanto não tiver todas as informações obrigatórias, responda em texto natural."
        )

        assistant_message = await self.ai_client.create_chat_completion(
            system_prompt=system_prompt,
            user_message=command.user_message,
        )

        # If the AI response contains valid JSON matching the objective schema,
        # we treat it as a completed objective draft and persist it.
        parsed = self._extract_json(assistant_message)
        if parsed is None:
            return {"assistant_message": assistant_message}

        if not self._is_complete_payload(parsed):
            return {"assistant_message": assistant_message}

        try:
            title = str(parsed["titulo"]).strip()
            description = str(parsed["descricao"]).strip()
            due_date = self._parse_due_date(parsed["prazo"])
        except (KeyError, TypeError, ValueError):
            return {"assistant_message": assistant_message}

        if not title or not description:
            return {"assistant_message": assistant_message}

        command_obj = RegisterObjectiveCommand(title=title, description=description, due_date=due_date)
        await self.objective_service.register(command_obj, user_id=user_id)
        return {"assistant_message": assistant_message}

    def _extract_json(self, text: str) -> Any:
        """Attempt to parse JSON from an AI response.

        Tolerates markdown code fences (`` ```json ... ``` ``) and
        surrounding natural-language text.  Tries, in order:
        1. Parsing the whole message as-is.
        2. Parsing the contents of a code fence.
        3. Parsing the substring between the first ``{`` and last ``}``.

        Returns the parsed value on success, or ``None`` if no valid
        JSON could be extracted.
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

        return None

    def _is_complete_payload(self, payload: Any) -> bool:
        """Validate the AI payload structure before it is treated as a completion."""
        if not isinstance(payload, dict):
            return False
        if not {"titulo", "descricao", "prazo"}.issubset(payload.keys()):
            return False
        return all(isinstance(payload[key], str) and bool(str(payload[key]).strip()) for key in ("titulo", "descricao", "prazo"))

    def _parse_due_date(self, value: str) -> datetime:
        """Parse the AI-supplied due date into a timezone-aware datetime."""
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return parsed.replace(tzinfo=UTC)
