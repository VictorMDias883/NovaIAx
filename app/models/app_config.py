"""
ORM model for the ``app_config`` table — the remote app configuration.

The mobile app reads this table (through ``GET /api/v1/app-config``) at
startup to learn things that must be changeable **without shipping a new
build**: whether the backend is in maintenance mode, the minimum and
recommended app versions (and where to update), the per-module feature
flags, and operator announcements.

Every row is a single ``key -> JSON value`` pair.  The set of valid keys,
their types and their fallback defaults live in
:mod:`app.services.app_config_service`; an unknown key is never persisted, so
a typo cannot silently introduce a config the app does not understand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONValue


class AppConfig(Base):
    """One remote-configuration entry (a typed key with a JSON value).

    Table: ``app_config``

    Attributes:
        id: Primary key.
        key: Stable configuration key understood by the app.
        value: The JSON-encoded value (bool / str / int / list / object).
        updated_by: Admin user who last changed the value (FK to ``users.id``).
        updated_at: Last change timestamp.
    """

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    value: Mapped[Any | None] = mapped_column(JSONValue, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
