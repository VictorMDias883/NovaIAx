"""
API v1 medication-care endpoints.

All routes live under ``/me`` and operate only on the caller's own rows.  The
backend records medications and their intakes and computes adherence on read;
it never diagnoses and never changes a dose.  List endpoints are paginated
(``page``/``limit``) and return a total.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_db_user
from app.models.health import IntakeStatus
from app.schemas.medication_schemas import (
    AdherenceResponse,
    IntakeCreate,
    IntakeListResponse,
    IntakeResponse,
    MedicationCreate,
    MedicationListResponse,
    MedicationResponse,
    MedicationUpdate,
)
from app.services.medication_service import MedicationService
from app.utils.request import current_user_id

router = APIRouter(prefix="/me", tags=["medication"])


@router.get("/medications", response_model=MedicationListResponse)
async def list_medications(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    active: bool | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MedicationListResponse:
    rows, total = await MedicationService(session).list_medications(
        current_user_id(current_user), page=page, limit=limit, active=active
    )
    return MedicationListResponse(
        items=[MedicationResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.post("/medications", response_model=MedicationResponse, status_code=201)
async def create_medication(
    payload: MedicationCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MedicationResponse:
    medication = await MedicationService(session).create_medication(current_user_id(current_user), payload)
    return MedicationResponse.model_validate(medication)


@router.get("/medications/adherence", response_model=AdherenceResponse)
async def medication_adherence(
    days: int = Query(30, ge=1, le=365),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> AdherenceResponse:
    data = await MedicationService(session).adherence(current_user_id(current_user), days=days)
    return AdherenceResponse(**data)


@router.get("/medications/{medication_id}", response_model=MedicationResponse)
async def get_medication(
    medication_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MedicationResponse:
    medication = await MedicationService(session).get_medication(current_user_id(current_user), medication_id)
    return MedicationResponse.model_validate(medication)


@router.patch("/medications/{medication_id}", response_model=MedicationResponse)
async def update_medication(
    payload: MedicationUpdate,
    medication_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MedicationResponse:
    medication = await MedicationService(session).update_medication(
        current_user_id(current_user), medication_id, payload
    )
    return MedicationResponse.model_validate(medication)


@router.delete("/medications/{medication_id}", status_code=204)
async def delete_medication(
    medication_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await MedicationService(session).delete_medication(current_user_id(current_user), medication_id)


@router.post("/medications/{medication_id}/intakes", response_model=IntakeResponse, status_code=201)
async def log_intake(
    payload: IntakeCreate,
    medication_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> IntakeResponse:
    intake = await MedicationService(session).log_intake(current_user_id(current_user), medication_id, payload)
    return IntakeResponse.model_validate(intake)


@router.get("/medication-intakes", response_model=IntakeListResponse)
async def list_intakes(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    medication_id: int | None = None,
    status: IntakeStatus | None = None,
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> IntakeListResponse:
    rows, total = await MedicationService(session).list_intakes(
        current_user_id(current_user),
        page=page,
        limit=limit,
        medication_id=medication_id,
        status=status,
        from_at=from_at,
        to_at=to_at,
    )
    return IntakeListResponse(
        items=[IntakeResponse.model_validate(row) for row in rows],
        page=page,
        limit=limit,
        total=total,
    )


@router.delete("/medication-intakes/{intake_id}", status_code=204)
async def delete_intake(
    intake_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await MedicationService(session).delete_intake(current_user_id(current_user), intake_id)
