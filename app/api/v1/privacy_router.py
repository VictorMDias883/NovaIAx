"""
API v1 privacy & compliance endpoints.

This router exposes the user-facing side of the product's data-stewardship
promises:

    - ``GET    /me/consents``        — read the caller's consent log.
    - ``PUT    /me/consents``        — grant/re-acknowledge a consent type.
    - ``DELETE /me/consents/{type}`` — withdraw a consent.
    - ``GET    /me/export``          — download a portable JSON bundle of every
      account-based record (rate-limited per user and per IP).
    - ``DELETE /me``                 — hard-delete the account and all owned
      data, guarded by the account password.

The routes live under the v1 API (no extra prefix) and are mounted in
:mod:`app.api.v1.router`; they require a database-backed identity
(:func:`app.api.deps.require_db_user`), so API-key ``SERVICE`` identities
are rejected.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis_client, get_session, require_db_user
from app.cache.redis_client import RedisClient
from app.models.consent import CONSENT_TYPES
from app.schemas.privacy import (
    AccountDeletionRequest,
    AccountDeletionResponse,
    ConsentListResponse,
    ConsentRecordSchema,
    ConsentUpdateRequest,
    ConsentWithdrawalResponse,
    DataExportResponse,
)
from app.services.privacy_service import (
    AccountDeletionService,
    AccountExportService,
    ConsentService,
    ExportRateLimiter,
)

router = APIRouter(prefix="", tags=["privacy"])


@router.get("/me/export", response_model=DataExportResponse)
async def export_my_data(
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    current_user: dict = Depends(require_db_user),
) -> DataExportResponse:
    """Return a portable JSON bundle of the caller's entire account.

    The bundle (content-type ``application/json``) is meant to be consumed by
    the user, not machines: it is a plain, pre-serialized document with no
    secrets — neither the password hash nor any purchase-token hash is ever
    included.

    The endpoint is throttled per user on top of the global per-IP bucket; a
    caller that exhausts the hourly budget receives a 429.

    Args:
        session: Database session (injected).
        redis_client: Shared :class:`RedisClient`, backer of the per-user
            export limiter (injected).
        current_user: The authenticated database-backed identity (injected).

    Returns:
        A dictionary representation of the export bundle.
    """
    user_id = int(str(current_user["id"]))
    limiter = ExportRateLimiter(redis_client)
    if not await limiter.allow(user_id):
        raise HTTPException(
            status_code=429,
            detail="Limite de exportação atingido para esta hora. Tente novamente mais tarde.",
        )
    bundle = await AccountExportService(session, redis_client=redis_client).build_bundle(user_id)
    await limiter.record(user_id)
    return DataExportResponse.model_validate(bundle)


@router.delete("/me", response_model=AccountDeletionResponse)
async def delete_my_account(
    payload: AccountDeletionRequest,
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    current_user: dict = Depends(require_db_user),
) -> AccountDeletionResponse:
    """Hard-delete the caller's account and all owned data.

    ``password`` must match the account's current password: a leaked access
    token alone must never be able to wipe an account.  A wrong password is a
    400 (never an ambiguous 401), mirroring the password-change route.

    All owned rows and Redis keys are removed in a single transaction; when
    the account has a Play subscription, the response carries Google's
    account-managed subscription-cancellation URL so the user can stop
    billing without contacting support.

    Args:
        payload: The account password.
        session: Database session (injected).
        redis_client: Shared :class:`RedisClient` for the post-transaction
            Redis purge (injected).
        current_user: The authenticated database-backed identity (injected).

    Returns:
        A confirmation message, plus the Play subscription-management URL
        when a subscription exists.

    Raises:
        HTTPException(400): If the password does not verify.
    """
    user_id = int(str(current_user["id"]))
    result = await AccountDeletionService(session, redis_client).delete_account(
        user_id, payload.password
    )
    return AccountDeletionResponse(**result)


@router.get("/me/consents", response_model=ConsentListResponse)
async def list_consents(
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_db_user),
) -> ConsentListResponse:
    """Return the caller's consent log."""
    consents = await ConsentService(session).list(int(str(current_user["id"])))
    return ConsentListResponse(consents=consents)


@router.put("/me/consents", response_model=ConsentRecordSchema)
async def update_consent(
    payload: ConsentUpdateRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_db_user),
) -> ConsentRecordSchema:
    """Grant (or re-acknowledge) one consent type.

    ``terms`` and ``privacy`` are recorded automatically at registration and
    re-acknowledged through this endpoint whenever their version bumps; the
    remaining types (``microphone``, ``contacts``, ``location``, ``learning``)
    are opt-in and only ever appear here.
    """
    record = await ConsentService(session).record(
        int(str(current_user["id"])), payload.type, payload.version
    )
    return ConsentRecordSchema(
        type=record.type,
        version=record.version,
        accepted_at=record.accepted_at,
    )


@router.delete("/me/consents/{consent_type}", response_model=ConsentWithdrawalResponse)
async def withdraw_consent(
    consent_type: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(require_db_user),
) -> ConsentWithdrawalResponse:
    """Withdraw one consent type.

    Withdrawal deletes the row: the app must stop collecting/using that data
    from that moment on.  An unknown type is a 404 (the caller asked to
    withdraw something they never granted).
    """
    if consent_type not in CONSENT_TYPES:
        raise HTTPException(status_code=404, detail="Tipo de consentimento não encontrado.")
    withdrawn = await ConsentService(session).withdraw(
        int(str(current_user["id"])), consent_type
    )
    if not withdrawn:
        raise HTTPException(status_code=404, detail="Tipo de consentimento não encontrado.")
    return ConsentWithdrawalResponse(message="Consentimento retirado.")
