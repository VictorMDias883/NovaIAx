"""
ORM model for the ``consent_records`` table.

GDPR-style audit trail of every consent a user has given.  One row per
``(user_id, type)``; re-accepting a newer version *updates* the row's
``version`` and ``accepted_at`` rather than appending, so the log always
holds the user's current stance.  Withdrawing a consent deletes the row,
which is indistinguishable from "never accepted" to the rest of the app.

Supported consent types are the fixed set the product collects:
``terms``, ``privacy``, ``microphone``, ``contacts``, ``location`` and
``learning`` (see :data:`CONSENT_TYPES`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: The consent types the product collects.  ``terms`` and ``privacy`` are
#: recorded automatically at registration; the rest are opt-in extras the
#: mobile app writes through ``PUT /me/consents``.
CONSENT_TYPES: frozenset[str] = frozenset(
    {"terms", "privacy", "microphone", "contacts", "location", "learning"}
)

#: Consent types accepted implicitly when an account is created.
DEFAULT_CONSENT_TYPES: tuple[str, ...] = ("terms", "privacy")


class ConsentRecord(Base):
    """ORM model representing one user's current stance on one consent type.

    Table: ``consent_records``
    """

    __tablename__ = "consent_records"
    __table_args__ = (UniqueConstraint("user_id", "type", name="uq_consent_records_user_id_type"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConsentEvent(Base):
    """Immutable audit event for a consent grant, renewal, or withdrawal."""

    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
