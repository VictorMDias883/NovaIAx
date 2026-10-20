"""
ORM model for the ``promo_codes`` table.

A promo code is an operator-created voucher the user redeems in the app.  It
never changes a price and never takes a payment — inside the Google Play model
the only thing a code may do is **grant a complimentary entitlement**:

``grant_plan``
    Grant a paid plan (``starter``/``pro``/``premium``) for
    ``duration_days`` days.
``extend_trial``
    Keep the new-user trial entitlement alive for ``duration_days`` days.
``bonus_quota``
    Add ``bonus_messages`` to the user's daily AI budget until the end date.

The code is stored **normalised to lower case** so lookups are
case-insensitive regardless of the database collation.  Redemption counts and
per-user limits live in :class:`~app.models.promo_redemption.PromoRedemption`
and are enforced under a row lock at redemption time.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class PromoCodeKind(str, Enum):
    """What redeeming a code grants."""

    GRANT_PLAN = "grant_plan"
    EXTEND_TRIAL = "extend_trial"
    BONUS_QUOTA = "bonus_quota"


#: Plan values a code may grant (includes ``trial`` for ``extend_trial``).
GRANTABLE_PLANS: frozenset[str] = frozenset({"trial", "starter", "pro", "premium"})


class PromoCode(Base):
    """ORM model representing a redeemable promotional code.

    Table: ``promo_codes``

    Attributes:
        id: Primary key.
        code: The normalised (lower-case) code the user types.
        kind: One of :class:`PromoCodeKind`.
        plan: Plan granted (``grant_plan``/``extend_trial``), else ``None``.
        duration_days: How long the grant lasts, in days.
        bonus_messages: Extra daily AI messages (``bonus_quota``).
        max_redemptions: Total redemption ceiling (``None`` = unlimited).
        per_user_limit: Redemptions allowed per user (default 1).
        valid_from/valid_until: Optional redemption window.
        active: Whether the code may currently be redeemed.
        note: Operator note.
        created_by: Admin user who created it (FK to ``users.id``).
        created_at: Row creation timestamp.
    """

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bonus_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_user_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
