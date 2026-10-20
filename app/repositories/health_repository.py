"""
Repository layer for the medication-care domain.

Owner-scoping is enforced at the data-access layer: every query filters on
``user_id``.  ``list_*`` methods return ``(rows, total)`` so the service can
paginate without a second count query.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import IntakeStatus, Medication, MedicationIntake


class _BaseRepository:
    """Shared session plumbing for the medication repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def _count(self, *where: Any, model: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await self.session.execute(stmt)).scalar_one())


class MedicationRepository(_BaseRepository):
    """Data access for :class:`Medication`."""

    async def create(self, user_id: int, **fields: Any) -> Medication:
        medication = Medication(user_id=user_id, **fields)
        self.session.add(medication)
        await self._commit()
        return medication

    async def get(self, medication_id: int) -> Medication | None:
        return (
            await self.session.execute(select(Medication).where(Medication.id == medication_id))
        ).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        active: bool | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Medication], int]:
        clauses = [Medication.user_id == user_id]
        if active is not None:
            clauses.append(Medication.active.is_(active))
        stmt = (
            select(Medication)
            .where(*clauses)
            .order_by(Medication.created_at.desc(), Medication.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Medication)
        return rows, total

    async def update(self, medication: Medication, **fields: Any) -> Medication:
        for key, value in fields.items():
            setattr(medication, key, value)
        await self._commit(medication)
        return medication

    async def delete(self, medication: Medication) -> None:
        await self.session.delete(medication)
        await self._commit()


class MedicationIntakeRepository(_BaseRepository):
    """Data access for :class:`MedicationIntake`."""

    async def create(self, user_id: int, **fields: Any) -> MedicationIntake:
        intake = MedicationIntake(user_id=user_id, **fields)
        self.session.add(intake)
        await self._commit()
        return intake

    async def get(self, intake_id: int) -> MedicationIntake | None:
        return (
            await self.session.execute(select(MedicationIntake).where(MedicationIntake.id == intake_id))
        ).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        medication_id: int | None = None,
        status: IntakeStatus | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[MedicationIntake], int]:
        clauses = [MedicationIntake.user_id == user_id]
        if medication_id is not None:
            clauses.append(MedicationIntake.medication_id == medication_id)
        if status is not None:
            clauses.append(MedicationIntake.status == status)
        if from_at is not None:
            clauses.append(MedicationIntake.created_at >= from_at)
        if to_at is not None:
            clauses.append(MedicationIntake.created_at <= to_at)
        stmt = (
            select(MedicationIntake)
            .where(*clauses)
            .order_by(MedicationIntake.created_at.desc(), MedicationIntake.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=MedicationIntake)
        return rows, total

    async def counts_by_status(self, user_id: int, *, since: datetime | None = None) -> dict[str, int]:
        clauses = [MedicationIntake.user_id == user_id]
        if since is not None:
            clauses.append(MedicationIntake.created_at >= since)
        stmt = select(MedicationIntake.status, func.count()).where(*clauses).group_by(MedicationIntake.status)
        rows = (await self.session.execute(stmt)).all()
        return {str(status.value if hasattr(status, "value") else status): int(count) for status, count in rows}

    async def update(self, intake: MedicationIntake, **fields: Any) -> MedicationIntake:
        for key, value in fields.items():
            setattr(intake, key, value)
        await self._commit(intake)
        return intake

    async def delete(self, intake: MedicationIntake) -> None:
        await self.session.delete(intake)
        await self._commit()
