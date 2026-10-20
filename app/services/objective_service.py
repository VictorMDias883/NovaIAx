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

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient
from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, AITaskType, PlanTier
from app.commands.register_objective_command import RegisterObjectiveCommand
from app.core.logging import get_logger
from app.repositories.objective_repository import ObjectiveRepository
from app.services.roadmap_day_service import RoadmapDayService
from app.utils.coercion import coerce_int
from app.utils.datetime_utils import ensure_aware
from app.utils.json_extraction import extract_json

logger = get_logger(__name__)

if TYPE_CHECKING:
    from app.models.objective import Objective
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

    def __init__(self, session: AsyncSession, ai_client: AIClient | AIRouter | None = None) -> None:
        """Initialise the service with a database session and an AI client.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
            ai_client: Optional AI client used to generate roadmaps.  If
                omitted, a :class:`GroqAIClient` is created lazily.
        """
        self.session = session
        self.ai_client = ai_client

    async def register(
        self,
        command: RegisterObjectiveCommand,
        user_id: int,
        *,
        plan_tier: PlanTier = PlanTier.FREE,
    ) -> dict[str, object]:
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
        due_date = ensure_aware(command.due_date)
        now = datetime.now(UTC)
        if due_date < now:
            raise HTTPException(status_code=400, detail="A data de prazo não pode estar no passado.")

        period_start = now
        # Inclusive last day of the window.  ``ROADMAP_WINDOW_DAYS`` is a count
        # of days, so the end is ``start + (count - 1)``.  Using ``+ count``
        # described eight dates to the model while only seven day rows were
        # created, so a hallucinated "Dia 8" could be accepted into the
        # markdown with no record behind it.
        period_end = min(period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS - 1), due_date)
        roadmap, day_contents = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=None,
            user_id=user_id,
            plan_tier=plan_tier,
        )

        repo = ObjectiveRepository(self.session)
        # The objective row and its roadmap days are written in ONE
        # transaction.  Committing them separately would let a failure on the
        # second insert leave a user with an objective that has a roadmap text
        # but zero (or a partial set of) tracking days — a state the renewal
        # window then refuses to repair for seven days.
        try:
            objective = await repo.create(
                title=command.title,
                description=command.description,
                due_date=due_date,
                user_id=user_id,
                roadmap=roadmap,
                roadmap_updated_at=now,
                commit=False,
            )
            days = await self._create_roadmap_days(
                objective.id,
                period_start,
                period_end,
                day_contents,
                commit=False,
            )
            await self.session.commit()
        except Exception:
            await self._rollback()
            # The AI output is the expensive part of this request and cannot be
            # regenerated without paying for it again, so it is logged rather
            # than discarded with the failed transaction.
            self._log_unpersisted_roadmap(
                action="register",
                user_id=user_id,
                title=command.title,
                roadmap=roadmap,
                day_contents=day_contents,
            )
            raise

        return self._to_dict(objective, days)

    async def list_by_user(self, user_id: int, offset: int = 0, limit: int = 50) -> list[dict[str, object]]:
        """Return all objectives belonging to a user, with their roadmap days.

        The roadmap days of every objective on the page are loaded with a
        single grouped query.  Fetching them one objective at a time made the
        number of database round-trips grow with the page size (1 + N), which
        is the N+1 problem: the user waits for N extra queries before a single
        pixel is rendered, and the load is multiplied by every other client.

        Args:
            user_id: ID of the user whose objectives are listed.
            offset: Number of records to skip (pagination).
            limit: Maximum number of objectives to return.

        Returns:
            A list of dictionaries, each representing an objective with
            its roadmap days, ordered by objective ID.
        """
        repo = ObjectiveRepository(self.session)
        objectives = await repo.list_by_user(user_id, offset=offset, limit=limit)

        from app.repositories.roadmap_day_repository import RoadmapDayRepository

        day_repo = RoadmapDayRepository(self.session)
        days_by_objective = await day_repo.list_by_objectives([obj.id for obj in objectives])
        return [self._to_dict(obj, days_by_objective.get(obj.id, [])) for obj in objectives]

    async def renew_roadmap(
        self,
        objective_id: int,
        user_id: int,
        role: str,
        *,
        plan_tier: PlanTier = PlanTier.FREE,
    ) -> dict[str, object]:
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
            raise HTTPException(status_code=404, detail="Objetivo não encontrado.")

        if str(user_id) != str(objective.user_id) and role != "ADMIN":
            raise HTTPException(status_code=403, detail="Não autorizado.")

        self._assert_renewal_allowed(objective)

        now = datetime.now(UTC)
        period_start = now
        due_date = ensure_aware(objective.due_date)
        # Inclusive last day of the window.  ``ROADMAP_WINDOW_DAYS`` is a count
        # of days, so the end is ``start + (count - 1)``.  Using ``+ count``
        # described eight dates to the model while only seven day rows were
        # created, so a hallucinated "Dia 8" could be accepted into the
        # markdown with no record behind it.
        period_end = min(period_start + timedelta(days=self.ROADMAP_WINDOW_DAYS - 1), due_date)
        command = RegisterObjectiveCommand(
            title=objective.title,
            description=objective.description,
            due_date=objective.due_date,
        )
        # Copied out of the ORM instance up front: the failure path below runs
        # after a rollback, which expires the instance, and touching an expired
        # attribute then triggers a lazy reload that raises ``MissingGreenlet``
        # — masking the real error and reporting a confusing 500 instead.
        objective_title = command.title
        # Also copied now, for the same reason: ``rollback`` below expires the
        # instance, and reading an expired attribute afterwards would trigger a
        # lazy reload that cannot run outside the event loop.
        previous_roadmap = objective.roadmap
        # Provide the AI with a short summary of the previous window's
        # per-day completion status so it can propose the next window
        # taking into account what the user completed or missed.
        from app.repositories.roadmap_day_repository import RoadmapDayRepository

        day_repo = RoadmapDayRepository(self.session)
        previous_days = await day_repo.list_by_objective(objective_id)
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

        # Everything the AI call needs is now a plain Python value, so the read
        # transaction opened above can be closed *before* the provider call.
        # Leaving it open would check out a pooled connection and hold it for
        # the whole provider wait (tens of seconds), which is precisely the
        # pool exhaustion this flow is meant to avoid; the comment on the
        # locking below assumes the connection is already back in the pool.
        await self._rollback()

        roadmap, day_contents = await self._generate_roadmap(
            command,
            period_start=period_start,
            period_end=period_end,
            previous_roadmap=previous_roadmap,
            previous_days_summary=previous_days_summary,
            user_id=user_id,
            plan_tier=plan_tier,
        )

        # The provider call above can take tens of seconds.  Re-reading the row
        # with ``FOR UPDATE`` *afterwards* — rather than locking before the AI
        # call — keeps a pooled database connection free for the whole wait
        # (the read transaction was closed above), while the window is still
        # re-checked inside the write transaction so two concurrent renewals
        # cannot both slip through the 7-day gate and overwrite each other's
        # roadmap (and duplicate the tracking days).  The locked read carries
        # ``populate_existing`` so it compares against the value the winner
        # committed, not the pre-AI snapshot this request loaded.
        try:
            locked = await repo.get_by_id(objective_id, for_update=True)
            if locked is None:
                raise HTTPException(status_code=404, detail="Objetivo não encontrado.")
            self._assert_renewal_allowed(locked)

            updated = await repo.update(
                objective_id,
                commit=False,
                # ``locked`` is already held FOR UPDATE by this transaction;
                # updating it in place avoids re-reading (and re-locking) it.
                objective=locked,
                roadmap=roadmap,
                roadmap_updated_at=now,
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="Objetivo não encontrado.")
            days = await self._create_roadmap_days(
                updated.id,
                period_start,
                period_end,
                day_contents,
                commit=False,
            )
            await self.session.commit()
        except Exception:
            await self._rollback()
            self._log_unpersisted_roadmap(
                action="renew",
                user_id=user_id,
                title=objective_title,
                objective_id=objective_id,
                roadmap=roadmap,
                day_contents=day_contents,
            )
            raise

        return self._to_dict(updated, days)

    def _assert_renewal_allowed(self, objective: "Objective") -> None:
        """Raise unless ``objective`` may have its roadmap renewed right now.

        Called twice during a renewal: once on the unlocked read that gathers
        the AI context, and once on the row locked for the write.  The second
        call is what makes the check authoritative against concurrent
        requests.

        Raises:
            HTTPException(409): If fewer than :attr:`ROADMAP_WINDOW_DAYS` have
                elapsed since the last generation.
            HTTPException(400): If the objective's due date has passed.
        """
        now = datetime.now(UTC)
        last_update = objective.roadmap_updated_at or objective.created_at
        last_update = ensure_aware(last_update)
        if now < last_update + timedelta(days=self.ROADMAP_WINDOW_DAYS):
            next_window = last_update + timedelta(days=self.ROADMAP_WINDOW_DAYS)
            raise HTTPException(
                status_code=409,
                detail=f"O roadmap só pode ser renovado a cada {self.ROADMAP_WINDOW_DAYS} dias. "
                f"Disponível a partir de {next_window.isoformat()}",
            )

        if ensure_aware(objective.due_date) < now:
            raise HTTPException(status_code=400, detail="Objetivo já expirou.")

    async def _rollback(self) -> None:
        """Discard every pending change in the current transaction.

        After a failed ``flush``/``commit`` the session is left in a state
        where the next statement would be executed inside an already-aborted
        transaction.  The session is reused per request, so rolling back here
        keeps the failure contained to the one endpoint that caused it.
        """
        try:
            await self.session.rollback()
        except Exception:
            logger.exception("Failed to roll back the objective transaction")

    def _log_unpersisted_roadmap(
        self,
        *,
        action: str,
        user_id: int,
        title: str,
        roadmap: str | None,
        day_contents: dict[int, str] | None,
        objective_id: int | None = None,
    ) -> None:
        """Record an AI roadmap that could not be persisted.

        Generating the roadmap costs a full AI round-trip; throwing it away
        because the database hiccupped wastes the user's tokens and leaves
        them with nothing.  It is logged in full at ``warning`` level so the
        text remains recoverable from the logs (or a log shipper) even though
        the transaction was rolled back.
        """
        logger.warning(
            "AI roadmap generated for %s but NOT persisted; keeping it for recovery. "
            "objective_id=%s user_id=%s title=%r roadmap_chars=%d",
            action,
            objective_id,
            user_id,
            title,
            len(roadmap or ""),
            extra={
                "unpersisted_roadmap": roadmap,
                "unpersisted_day_contents": day_contents,
                "unpersisted_action": action,
                "objective_id": objective_id,
                "user_id": user_id,
            },
        )

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

    async def _create_roadmap_days(
        self,
        objective_id: int,
        period_start: datetime,
        period_end: datetime,
        day_contents: dict[int, str] | None = None,
        commit: bool = True,
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
            commit: Whether to commit the days.  ``False`` keeps them in the
                caller's transaction so the objective and its days land
                together.

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
            commit=commit,
        )

    async def _generate_roadmap(
        self,
        command: RegisterObjectiveCommand,
        *,
        period_start: datetime,
        period_end: datetime,
        previous_roadmap: str | None,
        previous_days_summary: str | None = None,
        user_id: int | str | None = None,
        plan_tier: PlanTier = PlanTier.FREE,
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
        if self.ai_client is None:
            raise RuntimeError("ObjectiveService needs an AI client to generate a roadmap")
        client = self.ai_client
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
        assistant = await run_ai_request(
            client,
            AIRequest(
                system_prompt=system_prompt,
                user_message=user_message,
                user_id=user_id,
                task_type=AITaskType.ROADMAP,
                plan_tier=plan_tier,
            ),
        )
        raw = assistant.text

        parsed = extract_json(raw, context="AI roadmap generation", warn_on_plain_text=True)
        day_contents: dict[int, str] | None = None
        if isinstance(parsed, dict) and isinstance(parsed.get("days"), list):
            # The window length is known here, so a "day" outside 1..window is
            # a hallucinated entry rather than a day of this roadmap.
            window_days = (
                ensure_aware(period_end).date() - ensure_aware(period_start).date()
            ).days + 1
            metas: dict[int, str] = {}
            day_lines: list[str] = []
            for item in parsed["days"]:
                if not isinstance(item, dict) or "day" not in item:
                    continue
                meta = str(item.get("meta") or "").strip()
                if not meta:
                    continue
                # `day` comes from a language model, so it may be missing, a
                # string, a float or plain nonsense.  Coercing it unguarded
                # turned a malformed response into a 500 on an endpoint the
                # user had already paid an AI round-trip for; an unusable
                # entry is skipped instead.
                day_number = coerce_int(item["day"])
                if day_number is None or not 1 <= day_number <= window_days:
                    logger.warning(
                        "Discarding AI roadmap entry with out-of-range day %r (expected 1..%s)",
                        item["day"],
                        window_days,
                    )
                    continue
                # A repeated day number must not silently overwrite the first
                # meta, or the roadmap would list the day twice.
                if day_number in metas:
                    logger.warning("Discarding duplicate AI roadmap entry for day %s", day_number)
                    continue
                metas[day_number] = meta
                day_lines.append(f"- Dia {day_number}: {meta}")
            if day_lines:
                day_contents = metas
                return "## Roadmap\n" + "\n".join(day_lines), day_contents

        return raw, None
