"""
ORM model for the ``promotions`` table.

A promotion is **informational only**.  Under Google Play's payments policy the
app may not change a price or sell an entitlement outside Play Billing, so a
promotion is a banner the app renders: a title, a message, an optional emoji or
image, and an optional call-to-action that sends the user to the plan screen,
the redeem screen, or an external URL.

Promotions are filtered server-side by the caller's entitlement (audience
``all`` / ``plan`` / ``groups``) and by their time window, then ordered by
``priority``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONList


class PromotionCtaTarget(str, Enum):
    """Where the card's call-to-action sends the user."""

    PLANS = "plans"
    REDEEM = "redeem"
    URL = "url"


class PromotionAudience(str, Enum):
    """Who should see the promotion."""

    ALL = "all"
    PLAN = "plan"
    GROUPS = "groups"


class Promotion(Base):
    """ORM model representing an informational promotion card.

    Table: ``promotions``

    Attributes:
        id: Primary key.
        title: Short headline (PT-BR).
        message: Body text (PT-BR).
        emoji: Optional single emoji shown with the title.
        image_url: Optional image URL (mutually exclusive with ``emoji``).
        cta_label: Optional call-to-action button label.
        cta_target: One of :class:`PromotionCtaTarget`.
        cta_url: External URL when ``cta_target == "url"``.
        audience: One of :class:`PromotionAudience`.
        audience_plans: Plan values the audience is limited to.
        audience_groups: Active group slugs the audience is limited to.
        starts_at/ends_at: Optional visibility window.
        active: Whether the promotion is enabled.
        priority: Higher sorts first.
        created_at: Row creation timestamp.
    """

    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(20), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cta_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cta_target: Mapped[str] = mapped_column(String(20), nullable=False, default="plans")
    cta_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audience: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    audience_plans: Mapped[list[str] | None] = mapped_column(JSONList, nullable=True)
    audience_groups: Mapped[list[str] | None] = mapped_column(JSONList, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
