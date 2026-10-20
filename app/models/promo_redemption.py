"""
ORM model for the ``promo_redemptions`` table.

One row records that a user *received* an entitlement from a promo code or a
manual admin grant.  It is the durable ledger the entitlement resolver reads:
as long as a row has ``granted_until`` in the future (or ``None`` for a
permanent manual grant) the user keeps that plan/quota, independent of what
Google Play reports — RTDN events never touch these rows.

``code_id`` is ``None`` for a manual admin grant (``source="manual"``); the
unique ``(code_id, user_id)`` constraint enforces "one redemption per code per
user" for codes, while ``NULL`` values stay unconstrained so a user can hold
several manual grants.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PromoRedemptionSource(str, Enum):
    """Where a grant came from."""

    PROMO = "promo"
    MANUAL = "manual"


class PromoRedemption(Base):
    """ORM model representing one granted entitlement.

    Table: ``promo_redemptions``

    Attributes:
        id: Primary key.
        code_id: The redeemed :class:`~app.models.promo_code.PromoCode`
            (``None`` for a manual admin grant).
        user_id: The user who received the entitlement.
        source: ``promo`` or ``manual``.
        plan: The granted plan value, when the grant confers a plan.
        bonus_messages: Extra daily AI messages, when the grant is a quota
            bonus.
        granted_until: When the grant expires (``None`` = permanent).
        reason: Operator-facing reason (manual grants).
        created_by: Admin user who granted it, when applicable.
        redeemed_at: When the grant was recorded.
    """

    __tablename__ = "promo_redemptions"
    __table_args__ = (
        UniqueConstraint("code_id", "user_id", name="uq_promo_redemptions_code_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    code_id: Mapped[int | None] = mapped_column(
        ForeignKey("promo_codes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="promo")
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bonus_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
