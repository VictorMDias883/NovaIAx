"""
SQLAlchemy ORM model for the ``purchase_events`` table.

An append-only audit trail of everything billing-related: each client
verification (``POST /billing/google/verify``) and each Google Pub/Sub Real
Time Developer Notification (RTDN) webshooks into a row here.  This answers
"why does this user have this entitlement?" without relying on application
logs, and — crucially — stores only *sanitized* payloads.

The raw Play ``purchaseToken`` is stripped before persistence; only its
SHA-256 hash is kept, matching :class:`app.models.subscription.Subscription`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.db.types import JSONDict

if TYPE_CHECKING:
    from app.models.subscription import Subscription


class PurchaseEvent(Base):
    """ORM model representing one billing audit event.

    Table: ``purchase_events``

    Attributes:
        id: Primary key (auto-incremented integer).
        user_id: Owning user, when known (RTDN for an unknown token is still
            recorded with ``user_id = None``).
        subscription_id: The related :class:`Subscription` row, when one
            exists at the time of the event.
        event_type: Short machine label (``verify``, ``renewed``, ``canceled``,
            ``expired``, ``grace_period``, ``on_hold``, ``revoked``,
            ``paused``, ``recovered``, ``unknown`` ...).
        notification_type: The raw RTDN ``notificationType`` (``None`` for
            client verifications).
        product_id: Play product id, when the event reports one.
        purchase_token_hash: SHA-256 of the purchase token the event concerns.
        payload: Sanitized JSON copy of the source event (no raw token).
        created_at: Timestamp of when the row was written.
        subscription: The related :class:`Subscription`, when linked.
    """

    __tablename__ = "purchase_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True, nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    notification_type: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purchase_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONDict, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    subscription: Mapped[Subscription | None] = relationship(back_populates="purchase_events")
