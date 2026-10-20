"""
ORM model for the ``deletion_requests`` table.

Queue of account-deletion requests submitted from the public
``/account-deletion`` form when the requester cannot (or does not want to)
prove ownership with a password.  Support operators resolve these by hand:
either by helping the user delete via ``DELETE /api/v1/me`` or by removing
the account manually and marking the row ``handled``.

An account whose owner supplies the correct password is deleted
immediately and never lands here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: Lifecycle of a deletion request.
DELETION_STATUS_PENDING = "pending"
DELETION_STATUS_HANDLED = "handled"


class DeletionRequest(Base):
    """ORM model representing a pending account-deletion request.

    Table: ``deletion_requests``
    """

    __tablename__ = "deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DELETION_STATUS_PENDING)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
