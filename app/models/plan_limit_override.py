"""
ORM model for the ``plan_limit_overrides`` table.

:data:`app.clients.plan_limits.PLAN_LIMITS` is the code default for every
subscription ceiling.  This table lets an operator change a ceiling from the
admin panel **without a redeploy**: any non-null column overrides the code
default for that plan, and a null column keeps the default (so an override is
always partial and forward-compatible with new defaults).

Only the *operational* ceilings live here — daily message/token budget,
device-command budget, group/sub-agent counts, model tiers and feature flags.
Prices and trial length stay in the Google Play Console and are shown
read-only in the panel.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.db.types import JSONList


class PlanLimitOverride(Base):
    """Partial override of one plan's :class:`~app.clients.plan_limits.PlanLimits`.

    Table: ``plan_limit_overrides``.  Exactly one row per plan; every ceiling
    column is nullable and a null means "keep the code default".

    Attributes:
        id: Primary key.
        plan: Plan value (``free``/``trial``/``starter``/``pro``/``premium``).
        daily_messages/daily_tokens/daily_device_actions: Daily budgets;
            ``<= 0`` means unlimited (only ``daily_messages``/``daily_tokens``
            are treated as unlimited by :class:`PlanLimits`).
        max_groups/max_sub_agents: Active-entity ceilings (``<= 0`` unlimited).
        models: Ordered model-selection keys the plan may reach.
        features: Feature flags included in the plan.
        updated_by/updated_at: Change bookkeeping.
    """

    __tablename__ = "plan_limit_overrides"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    plan: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    daily_messages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_device_actions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_groups: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_sub_agents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    models: Mapped[list[str] | None] = mapped_column(JSONList, nullable=True)
    features: Mapped[list[str] | None] = mapped_column(JSONList, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def as_mapping(self) -> dict[str, Any]:
        """Return only the set (non-null, non-empty) override fields."""
        values: dict[str, Any] = {}
        for field in (
            "daily_messages",
            "daily_tokens",
            "daily_device_actions",
            "max_groups",
            "max_sub_agents",
        ):
            value = getattr(self, field)
            if value is not None:
                values[field] = value
        if self.models:
            values["models"] = tuple(self.models)
        if self.features:
            values["features"] = frozenset(self.features)
        return values
