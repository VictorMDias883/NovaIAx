"""
Server-side answer for ``WHAT_IS_PENDING_TODAY``.

The action is a *read*, not something the app can execute, so its result is
rendered by the backend straight into the reply and no proposal is ever
returned to the client (see
:data:`app.models.agent_action.SERVER_RESOLVED_ACTION_TYPES`).  The renderer
lives here so the device-command agent and the orchestrating agent produce
exactly the same text from the same rows.
"""

from datetime import UTC, date, datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.roadmap_day import RoadmapDayStatus
from app.repositories.objective_repository import ObjectiveRepository
from app.repositories.roadmap_day_repository import RoadmapDayRepository


class _PendingDay(NamedTuple):
    """A pending roadmap day with the objective title copied in.

    The relationship is not eagerly loaded, so reading ``day.objective``
    from async code would raise ``MissingGreenlet``.
    """

    day_number: int
    objective_title: str


class PendingTodayService:
    """Render "what is pending today" from the caller's own rows."""

    #: Cap on objectives scanned, so a user with a large history cannot
    #: produce an unbounded reply.
    MAX_OBJECTIVES = 50

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.objective_repo = ObjectiveRepository(session)
        self.day_repo = RoadmapDayRepository(session)

    async def render(self, user_id: int) -> str:
        """Answer "what is pending today" from the database.

        The reply is rendered here rather than taken from the model, so the
        answer reflects the rows that actually exist instead of what the
        model claims they are.
        """
        objectives = await self.objective_repo.list_by_user(user_id, 0, self.MAX_OBJECTIVES)
        if not objectives:
            return "Você ainda não cadastrou nenhum objetivo."

        # One query for every objective's days instead of one per objective.
        days_by_objective = await self.day_repo.list_by_objectives([o.id for o in objectives])

        today = self._today()
        pending_today: list[str] = []
        # (day_date, day) of the soonest pending day that is not today.
        next_pending: tuple[datetime, _PendingDay] | None = None

        for objective in objectives:
            for day in days_by_objective.get(objective.id, []):
                if day.status != RoadmapDayStatus.PENDING:
                    continue
                target = _PendingDay(
                    day_number=day.day_number,
                    objective_title=objective.title,
                )
                if day.day_date.date() == today:
                    pending_today.append(
                        f"«{objective.title}» dia {day.day_number}: "
                        f"{day.content or 'sem descrição'}"
                    )
                elif next_pending is None or day.day_date < next_pending[0]:
                    next_pending = (day.day_date, target)

        if not pending_today:
            text = "Não há dias pendentes para hoje."
            if next_pending is not None:
                soonest = next_pending[1]
                text += (
                    f" O próximo é o dia {soonest.day_number} de "
                    f"«{soonest.objective_title}», em {next_pending[0].date().isoformat()}."
                )
            return text

        bullets = "\n".join(f"- {entry}" for entry in pending_today)
        plural = "dia" if len(pending_today) == 1 else "dias"
        return f"Você tem {len(pending_today)} {plural} pendente(s) para hoje:\n{bullets}"

    @staticmethod
    def _today() -> date:
        """Return today's date in UTC, matching how roadmap days are stored.

        ``RoadmapDay.day_date`` is written as UTC midnight, so comparing
        against a local date would put days on the wrong side of midnight.
        """
        return datetime.now(UTC).date()
