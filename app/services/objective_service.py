"""
Service layer for objective management.

This :class:`ObjectiveService` handles the business logic for creating
and managing objectives (goals).  It sits between the API router and
the repository layer:

    Router → Command → Service → Repository → Database

An :class:`Objective` is the *general* goal of the user (e.g. "learning
English").  The roadmap lives *inside* the objective and decomposes it
into one basic meta / minimum objective per day (e.g. "day 1 — learn
basic vocabulary").  Those per-day metas are persisted on the
:class:`RoadmapDay` records.

Responsibilities:
    - Validate that the objective's ``due_date`` is not in the past.
    - Generate a 7-day roadmap for the objective using the AI provider,
      producing one basic meta per roadmap day.
    - Renew the roadmap every 7 days until the objective's due date.
    - Delegate persistence to :class:`ObjectiveRepository`.
    - Return plain dictionary representations of objectives.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient, GroqAIClient
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.core.logging import get_logger
from app.repositories.objective_repository import ObjectiveRepository
from app.services.roadmap_day_service import RoadmapDayService

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.models.roadmap_day import RoadmapDay


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
        due_date = self._ensure_aware(command.due_date)
        now = datetime.now(UTC)
        if due_date < now:
            raise HTTPException(status_code=400, detail="due_date cannot be in the past")

        period_start = now
        period_end = min(period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS), due_date)
        roadmap, day_contents = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=None,
        )

        repo = ObjectiveRepository(self.session)
        objective = await repo.create(
            title=command.title,
            description=command.description,
            due_date=due_date,
            user_id=user_id,
            roadmap=roadmap,
            roadmap_updated_at=now,
        )
        days = await self._create_roadmap_days(objective.id, period_start, period_end, day_contents)
        return self._to_dict(objective, days)

    async def list_by_user(self, user_id: int, offset: int = 0, limit: int = 50) -> list[dict[str, object]]:
        """Return all objectives belonging to a user, with their roadmap days.

        Args:
            user_id: ID of the user whose objectives are listed.
            offset: Number of records to skip (pagination).
            limit: Maximum number of objectives to return.

        Returns:
            A list of dictionaries, each representing an objective with
            its roadmap days.
        """
        repo = ObjectiveRepository(self.session)
        objectives = await repo.list_by_user(user_id, offset=offset, limit=limit)

        from app.repositories.roadmap_day_repository import RoadmapDayRepository

        day_repo = RoadmapDayRepository(self.session)
        result: list[dict[str, object]] = []
        for obj in objectives:
            days = await day_repo.list_by_objective(obj.id)
            result.append(self._to_dict(obj, days))
        return result

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
        # Provide the AI with a short summary of the previous window's
        # per-day completion status so it can propose the next window
        # taking into account what the user completed or missed.
        from app.repositories.roadmap_day_repository import RoadmapDayRepository

        day_repo = RoadmapDayRepository(self.session)
        previous_days = await day_repo.list_by_objective(objective.id)
        prev_summary_lines: list[str] = []
        if previous_days:
            completed = sum(1 for d in previous_days if d.status == d.status.COMPLETED)
            skipped = sum(1 for d in previous_days if d.status == d.status.SKIPPED)
            pending = sum(1 for d in previous_days if d.status == d.status.PENDING)
            prev_summary_lines.append(
                f"Resumo dos dias anteriores: {len(previous_days)} dia(s) — {completed} cumprido(s), {skipped} pulado(s), {pending} pendente(s)."
            )
            for d in previous_days:
                prev_summary_lines.append(
                    f"Dia {d.day_number}: {d.status.value} — {d.content or 'sem conteúdo'} ({d.day_date.date().isoformat()})"
                )
        previous_days_summary = "\n".join(prev_summary_lines) if prev_summary_lines else None

        roadmap, day_contents = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=objective.roadmap,
            previous_days_summary=previous_days_summary,
        )

        updated = await repo.update(
            objective_id,
            roadmap=roadmap,
            roadmap_updated_at=now,
        )
        days = await self._create_roadmap_days(updated.id, period_start, period_end, day_contents)
        return self._to_dict(updated, days)

    def _to_dict(self, objective, days: list["RoadmapDay"] | None = None) -> dict[str, object]:
        """Convert an :class:`Objective` instance into a plain dictionary.

        The ``days`` list carries the roadmap days (each with its basic
        meta / minimum objective) so the roadmap stays inside the
        objective in the API response.
        """
        return {
            "id": objective.id,
            "title": objective.title,
            "description": objective.description,
            "roadmap": objective.roadmap,
            "roadmap_updated_at": objective.roadmap_updated_at,
            "due_date": objective.due_date,
            "user_id": objective.user_id,
            "days": [
                {
                    "id": day.id,
                    "objective_id": day.objective_id,
                    "day_number": day.day_number,
                    "day_date": day.day_date,
                    "content": day.content,
                    "status": day.status,
                    "completed_at": day.completed_at,
                }
                for day in (days or [])
            ],
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
        day_contents: dict[int, str] | None = None,
    ) -> list["RoadmapDay"]:
        """Create one roadmap-day record per calendar day of the window.

        A roadmap window covers exactly :attr:`ROADMAP_WINDOW_DAYS`
        calendar days, so the day records span from ``period_start`` up
        to ``period_start + ROADMAP_WINDOW_DAYS - 1`` (capped by the
        objective's due date).  Delegates to :class:`RoadmapDayService`
        so that the tracking records stay in sync with the roadmap and
        carry the per-day basic metas.

        Args:
            objective_id: ID of the owning objective.
            period_start: Start of the window (timezone-aware).
            period_end: End of the window (timezone-aware).
            day_contents: Optional mapping of ``day_number`` → meta.

        Returns:
            The list of created :class:`RoadmapDay` instances.
        """
        window_end = min(
            period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS - 1),
            period_end,
        )
        return await RoadmapDayService(self.session).create_days(
            objective_id,
            period_start,
            window_end,
            contents=day_contents,
        )

    async def _generate_roadmap(
        self,
        command: RegisterObjectiveCommand,
        *,
        period_start: datetime,
        period_end: datetime,
        previous_roadmap: str | None,
        previous_days_summary: str | None = None,
    ) -> tuple[str, dict[int, str] | None]:
        """Generate a roadmap for a 7-day window via the AI provider.

        The general objective is decomposed into one basic meta / minimum
        objective per roadmap day.  The AI is asked to return a JSON
        payload (``{"days": [{"day": 1, "meta": "..."}, ...]}``) which is
        parsed into a markdown roadmap plus a ``day_number`` → meta
        mapping used to fill the :class:`RoadmapDay` records.

        Args:
            command: The objective's registration data.
            period_start: Start of the period covered by the roadmap.
            period_end: End of the period covered by the roadmap.
            previous_roadmap: The previous roadmap, or ``None`` for the
                first window.

        Returns:
            A ``(roadmap_markdown, day_contents)`` tuple.  ``day_contents``
            is ``None`` when the AI response could not be parsed as the
            expected JSON structure (the raw text is then kept as roadmap).

        Raises:
            HTTPException(502): If the AI provider is unavailable or
                fails to generate a roadmap.
        """
        client = self.ai_client or GroqAIClient()
        system_prompt = (
            "Você é um especialista em planejamento estratégico de metas. "
            "O objetivo do usuário é a meta geral (ex.: 'aprender inglês'), "
            "e o roadmap está contido dentro dele, decomposto em dias. "
            "As metas são executadas em janelas de 7 dias: o roadmap sempre "
            "cobre apenas o próximo período de 7 dias e é renovado quando "
            "essa janela termina. "
            "Para cada dia da janela, defina uma meta básica / objetivo mínimo "
            "e acionável (ex.: 'Dia 1: aprender vocabulário básico de saudações'). "
            "Responda EXCLUSIVAMENTE em JSON, sem markdown, no formato: "
            '{"days": [{"day": 1, "meta": "..."}, {"day": 2, "meta": "..."}]} '
            "com um item para cada dia do período informado. Use o roadmap "
            "anterior como contexto para dar continuidade ao plano."
        )
        user_message = (
            f"Objetivo geral: {command.title}\n"
            f"Descrição: {command.description or 'Não informada'}\n"
            f"Prazo final: {command.due_date.date().isoformat()}\n"
            f"Período do roadmap: {period_start.date().isoformat()} a {period_end.date().isoformat()}\n"
            f"Roadmap anterior (contexto):\n{previous_roadmap or 'Nenhum'}\n"
        )
        if previous_days_summary:
            user_message += f"\nContexto de cumprimento anterior:\n{previous_days_summary}\n"
        raw = await client.create_chat_completion(
            system_prompt=system_prompt,
            user_message=user_message,
        )

        parsed = self._extract_json(raw)
        day_contents: dict[int, str] | None = None
        if isinstance(parsed, dict) and isinstance(parsed.get("days"), list):
            metas: dict[int, str] = {}
            day_lines: list[str] = []
            for item in parsed["days"]:
                if not isinstance(item, dict) or "day" not in item:
                    continue
                meta = str(item.get("meta") or "").strip()
                if meta:
                    metas[int(item["day"])] = meta
                    day_lines.append(f"- Dia {item['day']}: {meta}")
            if day_lines:
                day_contents = metas
                return "## Roadmap\n" + "\n".join(day_lines), day_contents

        return raw, None

    @staticmethod
    def _extract_json(text: str) -> Any:
        """Attempt to parse JSON from an AI response.

        Tolerates markdown code fences (`` ```json ... ``` ``) and
        surrounding natural-language text.  Returns the parsed value on
        success, or ``None`` if no valid JSON could be extracted.
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(
                "AI roadmap response is not valid JSON; trying to extract embedded JSON",
                extra={"ai_response": text[:500]},
            )

        fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                logger.warning(
                    "AI roadmap code fence contains invalid JSON",
                    extra={"ai_response": fence_match.group(1)[:500]},
                )

        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                logger.warning(
                    "AI roadmap response contains no parseable JSON object; "
                    "objective will be created with the raw AI text and no per-day metas",
                    extra={"ai_response": text[:500]},
                )

        return None
