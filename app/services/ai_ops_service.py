"""
Service backing the admin "Operações de IA" page.

It joins three sources that were previously only visible in logs:

* the declared provider fleet (:attr:`Settings.ai_provider_catalog`) — is a
  provider configured at all, and is a key present?  (The key itself is never
  read or shown — only ``bool(api_key)``);
* the live routing policy (:data:`app.clients.ai_metrics.provider_policy`) and
  circuit-breaker state from the running router;
* the rolling request metrics (:data:`app.clients.ai_metrics.ai_metrics`) and
  the per-plan/per-day aggregate usage counters written by the quota tracker.

It also owns the write path: toggling a provider and reordering it, validated
against the declared fleet so a typo cannot invent a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_metrics import ai_metrics, provider_policy
from app.core.config import Settings, get_settings
from app.repositories.ai_provider_setting_repository import AIProviderSettingRepository

#: Redis key namespace for the per-plan/per-day aggregate usage counters.
USAGE_KEY_PREFIX = "ai_metrics:usage:"


async def refresh_provider_policy(session: AsyncSession) -> None:
    """Load the persisted provider overrides into the live policy singleton."""
    rows = await AIProviderSettingRepository(session).list_all()
    provider_policy.set_rows(
        [{"name": row.name, "enabled": row.enabled, "priority": row.priority} for row in rows]
    )


class AIOpsService:
    """Assemble and mutate the AI operations view."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        redis: Any | None = None,
        router: Any | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.redis = redis
        self.router = router
        self.repo = AIProviderSettingRepository(session)

    # -- Reads --------------------------------------------------------------

    def provider_overview(self) -> list[dict[str, Any]]:
        """Return one row per declared provider with its live status."""
        breakers: dict[str, bool] = {}
        if self.router is not None and hasattr(self.router, "breaker_states"):
            breakers = self.router.breaker_states()
        metrics = ai_metrics.snapshot()

        rows: list[dict[str, Any]] = []
        for config in self.settings.ai_provider_catalog:
            name = config.name
            rows.append(
                {
                    "name": name,
                    "base_url": config.base_url,
                    "default_model": config.default_model,
                    "models": dict(config.models),
                    "configured": bool(config.api_key),
                    "api_key_set": bool(config.api_key),
                    "declared_enabled": config.enabled,
                    "enabled": provider_policy.is_enabled(name, config.enabled),
                    "priority": provider_policy.priority(name, config.priority),
                    "has_override": provider_policy.has_override(name),
                    "in_router": name in breakers,
                    "breaker_open": breakers.get(name),
                    "metrics": metrics.get(
                        name,
                        {
                            "successes": 0,
                            "failures": 0,
                            "fallbacks": 0,
                            "skips": 0,
                            "samples": 0,
                            "latency_ms": {"avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0},
                            "recent_errors": [],
                            "last_success_at": None,
                            "last_error_at": None,
                        },
                    ),
                }
            )
        return sorted(rows, key=lambda row: (row["priority"], row["name"]))

    def total_fallbacks(self) -> int:
        """Total provider-to-provider fallbacks observed since start."""
        return ai_metrics.total_fallbacks()

    async def usage(self, *, days: int = 14) -> dict[str, Any]:
        """Return per-plan, per-day AI usage from the aggregate counters.

        Reads ``ai_metrics:usage:<plan>:<day>:messages|tokens`` for the last
        ``days`` UTC days and pivots them into ``{plan: {day: {...}}}``.
        """
        cutoff = (datetime.now(UTC).date() - timedelta(days=days - 1)).isoformat()
        usage: dict[str, dict[str, dict[str, int]]] = {}
        if self.redis is None:
            return {"days": days, "usage": usage}

        for key in await self.redis.scan_keys(f"{USAGE_KEY_PREFIX}*"):
            parts = key.split(":")
            if len(parts) != 5:
                continue
            _, _, plan, day, metric = parts
            if day < cutoff or metric not in {"messages", "tokens"}:
                continue
            bucket = usage.setdefault(plan, {}).setdefault(day, {"messages": 0, "tokens": 0})
            bucket[metric] = int(await self.redis.get(key) or 0)

        for plan_days in usage.values():
            for bucket in plan_days.values():
                bucket["messages"] = int(bucket["messages"])
                bucket["tokens"] = int(bucket["tokens"])
        return {"days": days, "usage": usage}

    # -- Writes -------------------------------------------------------------

    async def update_provider(
        self, name: str, *, enabled: bool, priority: int, admin_id: int | None
    ) -> None:
        """Persist a provider's enable/priority and apply it live."""
        catalog = {config.name: config for config in self.settings.ai_provider_catalog}
        if name not in catalog:
            raise HTTPException(status_code=404, detail="Provedor desconhecido.")
        await self.repo.upsert(name, enabled=enabled, priority=priority, updated_by=admin_id)
        await self.session.commit()
        await refresh_provider_policy(self.session)


__all__ = ["AIOpsService", "USAGE_KEY_PREFIX", "refresh_provider_policy"]
