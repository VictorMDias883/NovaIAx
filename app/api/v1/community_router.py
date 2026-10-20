"""
API v1 community-reporting endpoints.

Split in two surfaces:

* ``/community/...`` — the authenticated user-facing API (reports,
  confirmations, flags, blocks, official contacts, draft complaints);
* ``/community/support`` — a **read-only** directory of domestic-violence
  support lines.  Nothing a caller sends here is stored or published.

Photos are EXIF-stripped before being written to disk.  Draft complaints are
templates the user reviews and sends themselves; generation is plan-gated and
carries a disclaimer.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_ai_router,
    get_session,
    require_db_user,
    require_plan,
)
from app.clients.ai_router import AIRouter
from app.clients.plan_limits import FEATURE_AUTOMATION
from app.core.config import get_settings
from app.models.community import OfficialContactCategory, ReportCategory
from app.schemas.community_schemas import (
    BlockCreate,
    BlockListResponse,
    BlockResponse,
    ConfirmationResponse,
    DraftCreate,
    DraftGenerateRequest,
    DraftListResponse,
    DraftResponse,
    DraftUpdate,
    FlagCreate,
    FlagResponse,
    OfficialContactListResponse,
    OfficialContactResponse,
    ReportCreate,
    ReportListResponse,
    ReportResponse,
    ReportUpdate,
    SupportResourceListResponse,
    SupportResourceResponse,
)
from app.services.community_service import CommunityService
from app.services.subscription_service import Entitlement
from app.utils.image_exif import strip_metadata
from app.utils.request import current_user_id

router = APIRouter(prefix="/community", tags=["community"])

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png"}


# ---------------------------------------------------------------------------
# Support (read-only)
# ---------------------------------------------------------------------------
@router.get("/support", response_model=SupportResourceListResponse)
async def support_resources(
    _: dict = Depends(require_db_user),
) -> SupportResourceListResponse:
    return SupportResourceListResponse(
        items=[SupportResourceResponse(**resource) for resource in CommunityService.support_resources()]
    )


# ---------------------------------------------------------------------------
# Official contacts
# ---------------------------------------------------------------------------
@router.get("/official-contacts", response_model=OfficialContactListResponse)
async def list_official_contacts(
    category: OfficialContactCategory | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> OfficialContactListResponse:
    rows = await CommunityService(session).list_official_contacts(category=category)
    return OfficialContactListResponse(items=[OfficialContactResponse.model_validate(row) for row in rows])


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
@router.get("/reports", response_model=ReportListResponse)
async def list_reports(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: ReportCategory | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReportListResponse:
    views, total = await CommunityService(session).list_reports(
        current_user_id(current_user), page=page, limit=limit, category=category
    )
    return ReportListResponse(items=[ReportResponse(**view) for view in views], page=page, limit=limit, total=total)


@router.post("/reports", response_model=ReportResponse, status_code=201)
async def create_report(
    payload: ReportCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    service = CommunityService(session)
    report = await service.create_report(current_user_id(current_user), payload)
    return ReportResponse(**await service.get_report(current_user_id(current_user), report.id))


@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    view = await CommunityService(session).get_report(current_user_id(current_user), report_id)
    return ReportResponse(**view)


@router.patch("/reports/{report_id}", response_model=ReportResponse)
async def update_report(
    payload: ReportUpdate,
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    view = await CommunityService(session).update_report(current_user_id(current_user), report_id, payload)
    return ReportResponse(**view)


@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityService(session).delete_report(current_user_id(current_user), report_id)


@router.post("/reports/{report_id}/photo", response_model=ReportResponse)
async def upload_report_photo(
    report_id: int,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    settings = get_settings()
    if file.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Envie uma imagem JPEG ou PNG")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Arquivo vazio")
    if len(raw) > settings.community_max_upload_bytes:
        raise HTTPException(status_code=413, detail="Imagem muito grande")
    cleaned = strip_metadata(raw)
    suffix = ".png" if file.content_type == "image/png" else ".jpg"
    media_dir = Path(settings.community_media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{current_user_id(current_user)}_{uuid.uuid4().hex}{suffix}"
    (media_dir / filename).write_bytes(cleaned)
    service = CommunityService(session)
    await service.set_photo_path(current_user_id(current_user), report_id, filename)
    return ReportResponse(**await service.get_report(current_user_id(current_user), report_id))


@router.post("/reports/{report_id}/confirm", response_model=ConfirmationResponse)
async def confirm_report(
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ConfirmationResponse:
    data = await CommunityService(session).confirm_report(current_user_id(current_user), report_id)
    return ConfirmationResponse(**data)


@router.delete("/reports/{report_id}/confirm", response_model=ConfirmationResponse)
async def unconfirm_report(
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ConfirmationResponse:
    data = await CommunityService(session).unconfirm_report(current_user_id(current_user), report_id)
    return ConfirmationResponse(**data)


@router.post("/reports/{report_id}/flag", response_model=FlagResponse)
async def flag_report(
    payload: FlagCreate,
    report_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FlagResponse:
    data = await CommunityService(session).flag_report(current_user_id(current_user), report_id, payload.reason)
    return FlagResponse(**data)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
@router.get("/blocks", response_model=BlockListResponse)
async def list_blocks(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BlockListResponse:
    rows = await CommunityService(session).list_blocks(current_user_id(current_user))
    return BlockListResponse(
        items=[BlockResponse(blocked_id=row.blocked_id, created_at=row.created_at) for row in rows]
    )


@router.post("/blocks", response_model=BlockResponse, status_code=201)
async def block_user(
    payload: BlockCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BlockResponse:
    block = await CommunityService(session).block_user(current_user_id(current_user), payload.blocked_id)
    return BlockResponse(blocked_id=block.blocked_id, created_at=block.created_at)


@router.delete("/blocks/{blocked_id}", status_code=204)
async def unblock_user(
    blocked_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityService(session).unblock_user(current_user_id(current_user), blocked_id)


# ---------------------------------------------------------------------------
# Draft complaints
# ---------------------------------------------------------------------------
@router.get("/drafts", response_model=DraftListResponse)
async def list_drafts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DraftListResponse:
    views, total = await CommunityService(session).list_drafts(
        current_user_id(current_user), page=page, limit=limit
    )
    return DraftListResponse(items=[DraftResponse(**view) for view in views], page=page, limit=limit, total=total)


@router.post("/drafts", response_model=DraftResponse, status_code=201)
async def create_draft(
    payload: DraftCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DraftResponse:
    view = await CommunityService(session).create_draft(current_user_id(current_user), payload)
    return DraftResponse(**view)


@router.post("/drafts/generate", response_model=DraftResponse)
async def generate_draft(
    payload: DraftGenerateRequest,
    entitlement: Entitlement = Depends(require_plan(FEATURE_AUTOMATION)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> DraftResponse:
    view = await CommunityService(session, ai_client=ai_router).generate_draft(
        current_user_id(current_user), plan_tier=entitlement.plan, report_id=payload.report_id
    )
    return DraftResponse(**view)


@router.patch("/drafts/{draft_id}", response_model=DraftResponse)
async def update_draft(
    payload: DraftUpdate,
    draft_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DraftResponse:
    view = await CommunityService(session).update_draft(current_user_id(current_user), draft_id, payload)
    return DraftResponse(**view)


@router.delete("/drafts/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityService(session).delete_draft(current_user_id(current_user), draft_id)
