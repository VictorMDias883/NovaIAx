"""
Service layer for the medication-care domain.

Business rules:

* every row is owner-scoped — a medication/intake is only read or written for
  its own ``user_id``;
* **the backend never diagnoses and never changes a dose**; it only records
  what the user already takes and whether an intake was marked taken/skipped;
* adherence is computed on read from the recorded intakes (informational only).
* incoming naive datetimes are interpreted in the user's ``/me/agent``
  timezone and converted to UTC before persistence (the client sends ISO-8601).

    Router → Service → Repository → Database
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import IntakeStatus, Medication, MedicationIntake
from app.models.user_agent import UserAgent
from app.repositories.health_repository import (
    MedicationIntakeRepository,
    MedicationRepository,
)

#: Timezone used when the user has no profile or a malformed one.
DEFAULT_TIMEZONE = "America/Sao_Paulo"


def _as_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """Interpret a naive datetime in ``zone`` and return it in UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC)


class MedicationService:
    """CRUD and adherence for the caller's own medications."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.medications = MedicationRepository(session)
        self.intakes = MedicationIntakeRepository(session)

    async def _user_zone(self, user_id: int) -> ZoneInfo:
        """Return the user's IANA timezone (falling back to São Paulo)."""
        result = await self.session.execute(select(UserAgent.timezone).where(UserAgent.user_id == user_id))
        name = result.scalars().first() or DEFAULT_TIMEZONE
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TIMEZONE)

    # ------------------------------------------------------------------
    # Medications
    # ------------------------------------------------------------------
    async def create_medication(self, user_id: int, payload) -> Medication:  # noqa: ANN001 - Pydantic
        return await self.medications.create(
            user_id,
            name=payload.name,
            dosage=payload.dosage,
            frequency=payload.frequency,
            schedule_times=list(payload.schedule_times),
            start_date=payload.start_date,
            end_date=payload.end_date,
            instructions=payload.instructions,
            active=payload.active,
        )

    async def list_medications(
        self, user_id: int, *, page: int, limit: int, active: bool | None = None
    ) -> tuple[list[Medication], int]:
        return await self.medications.list_for_user(
            user_id, active=active, offset=(page - 1) * limit, limit=limit
        )

    async def _owned_medication(self, user_id: int, medication_id: int) -> Medication:
        medication = await self.medications.get(medication_id)
        if medication is None or medication.user_id != user_id:
            raise HTTPException(status_code=404, detail="Medicamento não encontrado")
        return medication

    async def get_medication(self, user_id: int, medication_id: int) -> Medication:
        return await self._owned_medication(user_id, medication_id)

    async def update_medication(self, user_id: int, medication_id: int, payload) -> Medication:  # noqa: ANN001
        medication = await self._owned_medication(user_id, medication_id)
        return await self.medications.update(medication, **payload.model_dump(exclude_unset=True))

    async def delete_medication(self, user_id: int, medication_id: int) -> None:
        await self.medications.delete(await self._owned_medication(user_id, medication_id))

    # ------------------------------------------------------------------
    # Intakes
    # ------------------------------------------------------------------
    async def log_intake(self, user_id: int, medication_id: int, payload) -> MedicationIntake:  # noqa: ANN001
        await self._owned_medication(user_id, medication_id)
        zone = await self._user_zone(user_id)
        taken_at = _as_utc(payload.taken_at, zone) if payload.taken_at is not None else None
        if payload.status is IntakeStatus.TAKEN and taken_at is None:
            taken_at = datetime.now(UTC)
        return await self.intakes.create(
            user_id,
            medication_id=medication_id,
            status=payload.status,
            scheduled_at=_as_utc(payload.scheduled_at, zone) if payload.scheduled_at is not None else None,
            taken_at=taken_at,
            notes=payload.notes,
        )

    async def list_intakes(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        medication_id: int | None = None,
        status: IntakeStatus | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> tuple[list[MedicationIntake], int]:
        if medication_id is not None:
            await self._owned_medication(user_id, medication_id)
        zone = await self._user_zone(user_id)
        return await self.intakes.list_for_user(
            user_id,
            medication_id=medication_id,
            status=status,
            from_at=_as_utc(from_at, zone) if from_at is not None else None,
            to_at=_as_utc(to_at, zone) if to_at is not None else None,
            offset=(page - 1) * limit,
            limit=limit,
        )

    async def delete_intake(self, user_id: int, intake_id: int) -> None:
        intake = await self.intakes.get(intake_id)
        if intake is None or intake.user_id != user_id:
            raise HTTPException(status_code=404, detail="Registro de medicação não encontrado")
        await self.intakes.delete(intake)

    # ------------------------------------------------------------------
    # Adherence
    # ------------------------------------------------------------------
    async def adherence(self, user_id: int, *, days: int = 30) -> dict[str, float | int]:
        since = datetime.now(UTC) - timedelta(days=days)
        counts = await self.intakes.counts_by_status(user_id, since=since)
        taken = counts.get(IntakeStatus.TAKEN.value, 0)
        skipped = counts.get(IntakeStatus.SKIPPED.value, 0)
        pending = counts.get(IntakeStatus.PENDING.value, 0)
        denominator = taken + skipped
        return {
            "days": days,
            "taken": taken,
            "skipped": skipped,
            "pending": pending,
            "total": taken + skipped + pending,
            "adherence_rate": round(taken / denominator, 4) if denominator else 0.0,
        }
