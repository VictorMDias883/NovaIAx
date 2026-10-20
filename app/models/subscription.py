"""
SQLAlchemy ORM model for the ``subscriptions`` table.

Exactly one row per user tracks the *current* Google Play subscription.  It is
a cache of the truth held by Google: the billing service always re-verifies a
purchase against the Developer API before trusting (or updating) a row, so a
stale or tampered row can never grant entitlement on its own.

The raw Play ``purchase_token`` is **never** stored — only
``purchase_token_hash`` (SHA-256) so a database leak cannot be replayed
against the API.  ``plan``/``status`` mirror the Play product and
``subscriptionState``; :class:`app.services.subscription_service.SubscriptionService`
translates them into a :class:`PlanLimits` whenever the app needs to gate a
feature.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.purchase_event import PurchaseEvent

from app.db.session import Base


class SubscriptionPlan(str, Enum):
    """The paid plans sold through Google Play.

    The free/trial tiers are never persisted: an absent or expired
    subscription simply falls back to them at entitlement-resolution time.
    """

    STARTER = "starter"
    PRO = "pro"
    PREMIUM = "premium"


class SubscriptionStatus(str, Enum):
    """Normalised lifecycle states, mapped from Play's ``subscriptionState``.

    ``ACTIVE`` and ``GRACE`` are the only states that grant paid features.
    ``CANCELED`` still grants features until ``expires_at`` (the user paid for
    the period); every other state is treated as not-entitled, which surfaces
    as a 403 ``subscription_inactive`` at the gate.
    """

    ACTIVE = "active"
    GRACE = "grace"
    ON_HOLD = "on_hold"
    PAUSED = "paused"
    CANCELED = "canceled"
    EXPIRED = "expired"


#: States that still grant paid features (``CANCELED`` is handled separately
#: because it keeps access only until ``expires_at``).
ENTITLED_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE}
)


class Subscription(Base):
    """ORM model representing a user's current subscription.

    Table: ``subscriptions``

    Attributes:
        id: Primary key (auto-incremented integer).
        user_id: Owning user (unique — one subscription row per user).
        plan: The :class:`SubscriptionPlan` purchased.
        status: Normalised :class:`SubscriptionStatus` from Play.
        product_id: The Google Play product id that was purchased.
        purchase_token_hash: SHA-256 of the Play purchase token (never the
            token itself).
        expires_at: When entitlement ends (Play ``expiryTime``); ``None`` when
            Google did not report one.
        auto_renewing: Whether Play reports the subscription will renew.
        is_trial: Whether the current offer is a free-trial offer.
        created_at/updated_at: Row bookkeeping timestamps.
        user: Owning :class:`~app.models.user.User`.
        purchase_events: Audit trail of verify/RTDN events for this row.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        SQLEnum(SubscriptionPlan, name="subscription_plan", native_enum=True),
        nullable=False,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        SQLEnum(SubscriptionStatus, name="subscription_status", native_enum=True),
        nullable=False,
        # The native PG enum is created with the member *names* as labels
        # (see migration ``d5f3a9b1c6e2``), which is also what SQLAlchemy
        # binds for a native ``Enum``.  The server default must therefore be
        # the name (``ACTIVE``) — using ``.value`` (``active``) produced a
        # default the enum type does not contain and broke ``create_all`` on
        # PostgreSQL.
        server_default=SubscriptionStatus.ACTIVE.name,
        index=True,
    )
    product_id: Mapped[str] = mapped_column(String(120), nullable=False)
    purchase_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renewing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    purchase_events: Mapped[list[PurchaseEvent]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
