"""
Pydantic schemas for the subscription / Google Play billing domain.

Covers the entitlement snapshot (``GET /me/subscription``), the client
purchase verification (``POST /billing/google/verify``) and the Pub/Sub push
acknowledgement (``POST /billing/google/rtdn``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.clients.ai_types import PlanTier
from app.clients.plan_limits import PlanLimits


class PlanLimitsResponse(BaseModel):
    """The ceilings and features included in a plan."""

    display_name: str
    price_brl: str | None = None
    max_groups: int
    max_sub_agents: int
    daily_messages: int
    daily_tokens: int
    daily_device_actions: int
    models: list[str]
    features: list[str]

    @classmethod
    def from_limits(cls, limits: PlanLimits) -> PlanLimitsResponse:
        """Build the response from a :class:`PlanLimits` instance."""
        return cls(
            display_name=limits.display_name,
            price_brl=limits.price_brl,
            max_groups=limits.max_groups,
            max_sub_agents=limits.max_sub_agents,
            daily_messages=limits.daily_messages,
            daily_tokens=limits.daily_tokens,
            daily_device_actions=limits.daily_device_actions,
            models=list(limits.models),
            features=sorted(limits.features),
        )


class SubscriptionResponse(BaseModel):
    """The authenticated user's current entitlement snapshot.

    ``plan`` is the *effective* tier (``free``/``trial``/``starter``/``pro``/
    ``premium``), which is what the client should render; ``status`` is the
    raw stored subscription state and ``subscription_inactive`` tells the app
    a renewal is needed.
    """

    plan: PlanTier
    status: str | None = None
    is_trial: bool
    active: bool
    subscription_inactive: bool
    has_subscription: bool
    product_id: str | None = None
    expires_at: datetime | None = None
    auto_renewing: bool
    source: str = "free"
    limits: PlanLimitsResponse


class GoogleVerifyRequest(BaseModel):
    """Request body for ``POST /billing/google/verify``.

    The client sends the Play product id and the purchase token it obtained
    from the Billing library; the server verifies the token against Google
    before granting anything.
    """

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=120)
    purchase_token: str = Field(min_length=1, max_length=4096)


class RtdnResponse(BaseModel):
    """Acknowledgement body for the Pub/Sub push endpoint."""

    status: str = "ok"
