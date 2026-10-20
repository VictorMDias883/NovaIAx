"""
API v1 router for subscriptions and Google Play billing.

Mounted at ``/api/v1``.  The user-facing endpoints (``GET /me/subscription``
and ``POST /billing/google/verify``) require a real database user; the Pub/Sub
push endpoint (``POST /billing/google/rtdn``) is authenticated by the shared
RTDN secret (or a Google-signed OIDC token) instead, because it is called by
Google, not by the app.
"""

from __future__ import annotations

import hmac
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_google_play_client, get_session, require_db_user
from app.clients.google_play_client import GooglePlayGateway
from app.core.config import get_settings
from app.core.logging import get_logger
from app.exceptions.plan_errors import PlanErrorCode, PlanRequiredError
from app.schemas.subscription_schemas import (
    GoogleVerifyRequest,
    PlanLimitsResponse,
    RtdnResponse,
    SubscriptionResponse,
)
from app.services.billing_service import BillingService, normalize_rtdn
from app.services.subscription_service import Entitlement, SubscriptionService
from app.utils.request import current_user_id

logger = get_logger(__name__)

router = APIRouter(tags=["billing"])

_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_JWKS_CACHE_TTL_SECONDS = 3600
_jwks_cache: tuple[float, dict[str, Any]] | None = None


def _entitlement_response(entitlement: Entitlement) -> SubscriptionResponse:
    """Render a :class:`SubscriptionResponse` from a resolved entitlement."""
    return SubscriptionResponse(
        plan=entitlement.plan,
        status=entitlement.status.value if entitlement.status is not None else None,
        is_trial=entitlement.is_trial,
        active=entitlement.active,
        subscription_inactive=entitlement.subscription_inactive,
        has_subscription=entitlement.has_subscription,
        product_id=entitlement.product_id,
        expires_at=entitlement.expires_at,
        auto_renewing=entitlement.auto_renewing,
        source=entitlement.source,
        limits=PlanLimitsResponse.from_limits(entitlement.limits),
    )


@router.get("/me/subscription", response_model=SubscriptionResponse)
async def get_my_subscription(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> SubscriptionResponse:
    """Return the caller's effective plan, status and limits."""
    entitlement = await SubscriptionService(session).resolve_entitlement(current_user_id(current_user))
    return _entitlement_response(entitlement)


@router.post("/billing/google/verify", response_model=SubscriptionResponse)
async def verify_google_purchase(
    payload: GoogleVerifyRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    google_client: GooglePlayGateway = Depends(get_google_play_client),
) -> SubscriptionResponse:
    """Verify a Play purchase server-side and activate the subscription.

    400 when the purchase token is unknown or the product maps to no plan;
    503 when Play billing is not configured or unreachable.
    """
    service = BillingService(session, google_client)
    await service.verify_and_activate(current_user_id(current_user), payload.product_id, payload.purchase_token)
    entitlement = await SubscriptionService(session).resolve_entitlement(current_user_id(current_user))
    return _entitlement_response(entitlement)


async def _verify_oidc_token(token: str, audience: str) -> bool:
    """Verify a Google-issued OIDC token against the published JWKS."""
    global _jwks_cache
    now = time.time()
    if _jwks_cache is None or now - _jwks_cache[0] > _JWKS_CACHE_TTL_SECONDS:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_GOOGLE_JWKS_URL)
        if response.status_code >= 400:
            return False
        _jwks_cache = (now, response.json())
    try:
        jwt.decode(
            token,
            _jwks_cache[1],
            algorithms=["RS256"],
            audience=audience,
            issuer=list(_GOOGLE_ISSUERS),
        )
    except Exception:  # noqa: BLE001 - any verification failure means "not Google"
        return False
    return True


async def _authorize_rtdn(request: Request) -> None:
    """Authenticate the Pub/Sub push request.

    Primary mechanism is the shared ``GOOGLE_PLAY_RTDN_TOKEN`` (constant-time
    compared), supplied either as ``?token=`` or ``X-RTDN-Token``.  When
    ``GOOGLE_PLAY_RTDN_AUDIENCE`` is configured, a Google-signed OIDC bearer
    token is also accepted.  If neither is configured the endpoint is closed
    with 503 so an unauthenticated deployment cannot be driven by forged
    notifications.
    """
    settings = get_settings()
    expected = settings.google_play_rtdn_token
    provided = request.query_params.get("token") or request.headers.get("x-rtdn-token")
    if expected and provided and hmac.compare_digest(expected, provided):
        return
    if settings.google_play_rtdn_audience:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
            if await _verify_oidc_token(token, settings.google_play_rtdn_audience):
                return
    if not expected and not settings.google_play_rtdn_audience:
        raise PlanRequiredError(
            PlanErrorCode.BILLING_UNAVAILABLE,
            detail="O endpoint de notificações de compra não está configurado.",
        )
    raise HTTPException(status_code=401, detail="Notificação não autenticada.")


@router.post("/billing/google/rtdn", response_model=RtdnResponse)
async def google_rtdn(
    request: Request,
    session: AsyncSession = Depends(get_session),
    google_client: GooglePlayGateway = Depends(get_google_play_client),
) -> RtdnResponse:
    """Receive a Real Time Developer Notification from Google Pub/Sub.

    Always returns ``200`` for well-formed messages so Pub/Sub does not retry
    forever; the authoritative state is re-fetched from the Play API before
    anything is written.
    """
    await _authorize_rtdn(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Corpo de notificação inválido.")
    notification = normalize_rtdn(body)
    if notification is None:
        return RtdnResponse(status="ignored")
    await BillingService(session, google_client).apply_rtdn(notification)
    return RtdnResponse()
