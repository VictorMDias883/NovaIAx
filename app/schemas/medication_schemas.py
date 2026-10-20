"""Pydantic schemas for the medication-care endpoints."""

from __future__ import annotations

import re
from datetime import date, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.health import IntakeStatus

#: ``"HH:MM"`` in 24-hour local time (00:00–23:59).
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: Upper bound on the number of scheduled times a medication may carry.
MAX_SCHEDULE_TIMES = 12


def _validate_schedule_times(times: list[str]) -> list[str]:
    """Validate and normalise a list of ``"HH:MM"`` local times.

    Every entry must match 24-hour ``HH:MM``; the list is de-duplicated while
    preserving the caller's order and sorted ascending so the schedule is
    deterministic.  More than :data:`MAX_SCHEDULE_TIMES` entries is rejected.
    """
    if len(times) > MAX_SCHEDULE_TIMES:
        raise ValueError(f"schedule_times aceita no máximo {MAX_SCHEDULE_TIMES} horários")
    cleaned: list[str] = []
    for value in times:
        text = value.strip()
        if not _TIME_RE.match(text):
            raise ValueError(f"Horário inválido: '{value}'. Use o formato HH:MM (24h).")
        if text not in cleaned:
            cleaned.append(text)
    return sorted(cleaned)


class MedicationCreate(BaseModel):
    """Request body for creating a medication."""

    name: str = Field(min_length=1, max_length=200)
    dosage: str | None = Field(default=None, max_length=120)
    frequency: str | None = Field(default=None, max_length=120)
    schedule_times: list[str] = Field(
        default_factory=list,
        description="Horários locais ('HH:MM') em que a dose é devida, no fuso do usuário.",
    )
    start_date: date = Field(description="Primeiro dia em que o horário vale.")
    end_date: date | None = Field(default=None, description="Último dia do horário (null = sem fim).")
    instructions: str | None = Field(default=None, max_length=5000)
    active: bool = True

    @field_validator("schedule_times")
    @classmethod
    def _check_times(cls, value: list[str]) -> list[str]:
        return _validate_schedule_times(value)

    @model_validator(mode="after")
    def _check_date_range(self) -> MedicationCreate:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date não pode ser anterior a start_date")
        return self


class MedicationUpdate(BaseModel):
    """Request body for patching a medication (all fields optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    dosage: str | None = Field(default=None, max_length=120)
    frequency: str | None = Field(default=None, max_length=120)
    schedule_times: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    instructions: str | None = Field(default=None, max_length=5000)
    active: bool | None = None

    @field_validator("schedule_times")
    @classmethod
    def _check_times(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_schedule_times(value)


class MedicationResponse(BaseModel):
    """Response body for a single medication."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    dosage: str | None = None
    frequency: str | None = None
    schedule_times: list[str] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    instructions: str | None = None
    active: bool
    created_at: datetime
    updated_at: datetime


class MedicationListResponse(BaseModel):
    """Paginated medication list."""

    items: list[MedicationResponse]
    page: int
    limit: int
    total: int


class IntakeCreate(BaseModel):
    """Request body for logging a medication intake.

    ``status`` defaults to ``taken`` because the most common client action is
    "I took it now"; a caller may also record ``skipped`` (with an optional
    note) or a ``pending`` scheduled dose.  The scheduled instant is accepted
    either as ``scheduled_at`` (legacy) or ``scheduled_for`` (preferred); naive
    datetimes are interpreted in the user's ``/me/agent`` timezone.
    """

    status: IntakeStatus = IntakeStatus.TAKEN
    scheduled_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("scheduled_at", "scheduled_for"),
    )
    taken_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)


class IntakeResponse(BaseModel):
    """Response body for a single intake."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    medication_id: int
    status: IntakeStatus
    scheduled_at: datetime | None = None
    taken_at: datetime | None = None
    notes: str | None = None
    created_at: datetime


class IntakeListResponse(BaseModel):
    """Paginated intake list."""

    items: list[IntakeResponse]
    page: int
    limit: int
    total: int


class AdherenceResponse(BaseModel):
    """Computed adherence over a window.

    ``adherence_rate`` is ``taken / (taken + skipped)`` rounded to 4 decimals,
    or ``0.0`` when there is nothing to divide by.  This is an informational
    metric only — it is never a clinical assessment.
    """

    days: int
    taken: int
    skipped: int
    pending: int
    total: int
    adherence_rate: float
