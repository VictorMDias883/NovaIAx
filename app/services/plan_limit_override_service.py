"""
Service for the per-plan ceiling overrides.

Owns three things:

* refreshing the process-wide snapshot that :func:`get_plan_limits` reads;
* assembling the effective (code default + override) view for the panel;
* validating and persisting an operator's change, then pushing it live.

Prices and trial length are **not** editable here: under Google Play's policy
they live in the Play Console and are shown read-only in the panel.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_types import PlanTier
from app.clients.plan_limits import ALL_FEATURES, get_plan_limits
from app.clients.plan_limits_overrides import plan_limit_overrides
from app.core.config import Settings, get_settings
from app.repositories.plan_limit_override_repository import PlanLimitOverrideRepository

#: The ceiling fields an operator may override (in panel display order).
EDITABLE_FIELDS: tuple[str, ...] = (
    "daily_messages",
    "daily_tokens",
    "daily_device_actions",
    "max_groups",
    "max_sub_agents",
)

#: The plan tiers shown (and editable) in the panel, cheapest first.
PANEL_PLANS: tuple[PlanTier, ...] = (
    PlanTier.FREE,
    PlanTier.TRIAL,
    PlanTier.STARTER,
    PlanTier.PRO,
    PlanTier.PREMIUM,
)


async def refresh_plan_limit_overrides(
    session: AsyncSession, settings: Settings | None = None, *, force: bool = False
) -> None:
    """Reload the in-memory override snapshot when it is stale (or forced)."""
    settings = settings or get_settings()
    if not force and not plan_limit_overrides.is_stale(settings.plan_limit_override_ttl_seconds):
        return
    rows = await PlanLimitOverrideRepository(session).list_all()
    plan_limit_overrides.set_rows({row.plan: row.as_mapping() for row in rows})


class PlanLimitOverrideService:
    """Read, validate and persist per-plan ceiling overrides."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.repo = PlanLimitOverrideRepository(session)

    # -- Reads --------------------------------------------------------------

    async def effective_rows(self) -> list[dict[str, Any]]:
        """Return one row per panel plan: its defaults and any override."""
        stored = {row.plan: row.as_mapping() for row in await self.repo.list_all()}
        rows: list[dict[str, Any]] = []
        for plan in PANEL_PLANS:
            default = get_plan_limits(self.settings, plan, apply_overrides=False)
            effective = get_plan_limits(self.settings, plan)
            override = stored.get(plan.value, {})
            rows.append(
                {
                    "plan": plan.value,
                    "display_name": default.display_name,
                    "price_brl": default.price_brl,
                    "defaults": {
                        **{field: getattr(default, field) for field in EDITABLE_FIELDS},
                        "models": list(default.models),
                        "features": sorted(default.features),
                    },
                    "override": override,
                    "effective": {
                        **{field: getattr(effective, field) for field in EDITABLE_FIELDS},
                        "models": list(effective.models),
                        "features": sorted(effective.features),
                    },
                }
            )
        return rows

    # -- Writes -------------------------------------------------------------

    async def update(self, plan: str, values: dict[str, Any], *, admin_id: int | None) -> None:
        """Persist ``values`` for ``plan`` and push the snapshot live."""
        await self.repo.upsert(plan, values, updated_by=admin_id)
        await self.session.commit()
        await refresh_plan_limit_overrides(self.session, self.settings, force=True)

    async def clear(self, plan: str, *, admin_id: int | None) -> None:
        """Drop every override for ``plan``, reverting to the code defaults."""
        empty = {field: None for field in EDITABLE_FIELDS}
        empty["models"] = None
        empty["features"] = None
        await self.repo.upsert(plan, empty, updated_by=admin_id)
        await self.session.commit()
        await refresh_plan_limit_overrides(self.session, self.settings, force=True)

    # -- Validation ---------------------------------------------------------

    @staticmethod
    def parse(form: dict[str, str]) -> dict[str, Any]:
        """Translate and validate a submitted override form.

        Blank fields mean "keep the code default" (stored as ``None``).
        Raises :class:`fastapi.HTTPException` (400) on malformed input.
        """
        values: dict[str, Any] = {}
        for field in EDITABLE_FIELDS:
            raw = (form.get(field) or "").strip()
            if raw == "":
                values[field] = None
                continue
            try:
                values[field] = int(raw)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Valor inválido para {field}: {raw!r}",
                ) from exc

        raw_models = (form.get("models") or "").strip()
        values["models"] = (
            [m.strip() for m in raw_models.split(",") if m.strip()] if raw_models else None
        )

        raw_features = (form.get("features") or "").strip()
        features = [f.strip() for f in raw_features.split(",") if f.strip()] if raw_features else []
        unknown = [f for f in features if f not in ALL_FEATURES]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Recursos desconhecidos: {', '.join(unknown)}")
        values["features"] = features or None
        return values

    @staticmethod
    def defaults_for(plan: PlanTier, settings: Settings | None = None) -> Any:
        """Return the effective limits for ``plan`` (used by the panel)."""
        return get_plan_limits(settings or get_settings(), plan)


__all__ = [
    "EDITABLE_FIELDS",
    "PANEL_PLANS",
    "PlanLimitOverrideService",
    "refresh_plan_limit_overrides",
]
