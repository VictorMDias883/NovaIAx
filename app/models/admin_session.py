"""
SQLAlchemy ORM model for the admin console's server-side sessions.

Table: ``admin_sessions``.

The admin panel does **not** reuse the mobile API's JWT cookies.  Instead it
issues an opaque random token stored in an httpOnly cookie; only the SHA-256
hash of that token is persisted, so a database leak cannot be replayed as a
valid cookie.  Every session row carries:

* the owning admin (``user_id``; cascade-deleted with the account),
* a per-session CSRF token compared on every state-changing request,
* an absolute ``expires_at`` cap and a ``last_seen_at`` idle clock,
* a ``revoked_at`` marker used for logout and for invalidating a session the
  moment the account's role or credentials change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AdminSession(Base):
    """A server-side admin panel session.

    Table: ``admin_sessions``.

    Attributes:
        id: Primary key (auto-incremented integer).
        user_id: Owning admin account (``users.id``).
        token_hash: SHA-256 hex digest of the opaque cookie token.
        csrf_token: Random token required on every mutating request.
        created_at: When the session was created.
        last_seen_at: Updated on every authenticated request (idle clock).
        expires_at: Absolute expiry; independent of activity.
        revoked_at: Set when explicitly logged out or force-invalidated.
        user_agent: Best-effort client fingerprint for the audit trail.
        ip_address: Best-effort source IP for the audit trail.
    """

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = ["AdminSession"]
