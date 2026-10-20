"""
Pydantic schemas for promo codes and informational promotions.

The app redeems a code (``POST /me/promo-codes/redeem``) and lists the
promotion cards addressed to it (``GET /promotions/active``).  The redeem
response echoes what was granted so the client can confirm in-place instead of
re-fetching the subscription.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.promotion import Promotion


class PromoRedeemRequest(BaseModel):
    """Request body for ``POST /me/promo-codes/redeem``."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=40)


class PromoRedeemResponse(BaseModel):
    """What a successful redemption granted."""

    code: str
    kind: str
    plan: str | None = None
    bonus_messages: int | None = None
    expires_at: datetime | None = None


class PromotionResponse(BaseModel):
    """One informational promotion card shown to a user."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    message: str
    emoji: str | None = None
    image_url: str | None = None
    cta_label: str | None = None
    cta_target: str
    cta_url: str | None = None
    priority: int

    @classmethod
    def from_model(cls, promotion: Promotion) -> PromotionResponse:
        """Build the response from a :class:`Promotion` ORM row."""
        return cls.model_validate(promotion)
