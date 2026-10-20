"""
API v1 router for promo codes and informational promotions.

Mounted at ``/api/v1``.  Both endpoints require a real database user.  The
redeem endpoint is rate-limited per user and per IP before it touches the
database; the promotions endpoint resolves the caller's entitlement and
personalization profile to filter the cards addressed to it.

Neither endpoint changes a price or takes a payment — they only record a
complimentary grant and return informational content, which is what the Google
Play payments policy allows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis_client, get_session, require_db_user
from app.cache.promo_rate_limiter import PromoRedeemRateLimiter
from app.cache.redis_client import RedisClient
from app.commands.redeem_promo_code_command import RedeemPromoCodeCommand
from app.core.config import get_settings
from app.core.network import get_client_ip
from app.schemas.promo_schemas import (
    PromoRedeemRequest,
    PromoRedeemResponse,
    PromotionResponse,
)
from app.services.promo_service import PromoService
from app.utils.request import current_user_id

router = APIRouter(tags=["promotions"])


@router.post("/me/promo-codes/redeem", response_model=PromoRedeemResponse, status_code=201)
async def redeem_promo_code(
    payload: PromoRedeemRequest,
    request: Request,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
) -> PromoRedeemResponse:
    """Redeem a promo code for the caller.

    Returns the granted plan/quota.  Stable failures: ``404 not_found``,
    ``410 expired``, ``409 exhausted``, ``409 already_redeemed``; ``429`` when
    the per-user/per-IP redemption budget is spent.
    """
    user_id = current_user_id(current_user)
    ip = get_client_ip(request)
    await PromoRedeemRateLimiter(redis_client, get_settings()).check(int(user_id), ip)
    return await PromoService(session, redis_client).redeem(
        RedeemPromoCodeCommand(code=payload.code, user_id=int(user_id)), ip=ip
    )


@router.get("/promotions/active", response_model=list[PromotionResponse])
async def list_active_promotions(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> list[PromotionResponse]:
    """List the informational promotion cards addressed to the caller."""
    promotions = await PromoService(session).list_active_promotions(int(current_user_id(current_user)))
    return [PromotionResponse.from_model(promotion) for promotion in promotions]
