"""
ORM model for the ``ai_provider_settings`` table.

The AI provider fleet — base URLs, API keys, pacing — is declared through
environment variables (``AI_PROVIDERS_JSON`` or the per-provider groups) and
is **never** stored or shown in the panel.  This table only carries the two
runtime knobs an operator may change without a redeploy:

* whether a configured provider takes part in routing at all, and
* its priority (lower is tried first).

The router reads these overrides from a short-TTL snapshot (see
:mod:`app.clients.ai_metrics`) so toggling a provider applies on the next
request.  Secrets stay in the environment.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AIProviderSetting(Base):
    """Runtime enable/priority override for one AI provider.

    Table: ``ai_provider_settings``

    Attributes:
        id: Primary key.
        name: The provider name (matches ``AIProviderConfig.name``).
        enabled: Whether the provider may be routed to.
        priority: Routing order (lower is tried first).
        updated_by/updated_at: Change bookkeeping.
    """

    __tablename__ = "ai_provider_settings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
