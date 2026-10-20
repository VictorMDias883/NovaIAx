"""
In-process AI provider metrics and routing policy.

The admin "Operações de IA" page needs to answer, at a glance: is each provider
configured, is it currently allowed to serve, is its circuit breaker open, how
slow is it, how often does it fail or get skipped, and which provider absorbed
the traffic when another fell over.  The AI router already knows all of this at
request time but historically threw it away.

Two process-wide singletons live here:

* :data:`ai_metrics` — rolling per-provider counters and latency/error windows,
  written by :class:`~app.clients.ai_router.AIRouter` on every attempt.
* :data:`provider_policy` — the operator's enable/priority overrides, consulted
  by the router to order and skip providers without a redeploy.  Secrets are
  never part of this; only ``enabled`` and ``priority``.

Both are best-effort and in-memory: they reset on restart, which is the right
semantics for "what is happening right now" and keeps the hot path free of I/O.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime
from typing import Any


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolation percentile over an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


class ProviderMetrics:
    """Rolling metrics for a single AI provider."""

    def __init__(self, *, window: int) -> None:
        self._window = max(10, window)
        self.successes = 0
        self.failures = 0
        self.fallbacks = 0
        self.skips = 0
        self._latencies: deque[float] = deque(maxlen=self._window)
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=20)
        self.last_success_at: float | None = None
        self.last_error_at: float | None = None

    def record_success(self, latency_ms: float) -> None:
        self.successes += 1
        self._latencies.append(latency_ms)
        self.last_success_at = time.time()

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.last_error_at = time.time()
        self._recent_errors.append(
            {"at": datetime.now(UTC).isoformat(), "reason": reason}
        )

    def record_fallback(self, reason: str) -> None:
        """A request this provider could not serve was retried elsewhere."""
        self.fallbacks += 1
        self._recent_errors.append(
            {"at": datetime.now(UTC).isoformat(), "reason": f"fallback:{reason}"}
        )

    def record_skip(self) -> None:
        self.skips += 1

    def snapshot(self) -> dict[str, Any]:
        latencies = sorted(self._latencies)
        return {
            "successes": self.successes,
            "failures": self.failures,
            "fallbacks": self.fallbacks,
            "skips": self.skips,
            "samples": len(latencies),
            "latency_ms": {
                "avg": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                "p50": round(_percentile(latencies, 0.50), 1),
                "p95": round(_percentile(latencies, 0.95), 1),
                "p99": round(_percentile(latencies, 0.99), 1),
            },
            "recent_errors": list(self._recent_errors),
            "last_success_at": (
                datetime.fromtimestamp(self.last_success_at, UTC).isoformat()
                if self.last_success_at
                else None
            ),
            "last_error_at": (
                datetime.fromtimestamp(self.last_error_at, UTC).isoformat()
                if self.last_error_at
                else None
            ),
        }


class AIMetrics:
    """Registry of per-provider :class:`ProviderMetrics`."""

    def __init__(self, *, window: int = 200) -> None:
        self._window = window
        self._providers: dict[str, ProviderMetrics] = {}

    def provider(self, name: str) -> ProviderMetrics:
        metrics = self._providers.get(name)
        if metrics is None:
            metrics = ProviderMetrics(window=self._window)
            self._providers[name] = metrics
        return metrics

    def reset(self) -> None:
        """Forget all metrics (used by tests between cases)."""
        self._providers = {}

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: metrics.snapshot() for name, metrics in self._providers.items()}

    def total_fallbacks(self) -> int:
        return sum(metrics.fallbacks for metrics in self._providers.values())


class ProviderPolicy:
    """Operator overrides for provider enable/priority."""

    def __init__(self) -> None:
        self._enabled: dict[str, bool] = {}
        self._priority: dict[str, int] = {}

    def reset(self) -> None:
        self._enabled = {}
        self._priority = {}

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self._enabled = {row["name"]: bool(row["enabled"]) for row in rows}
        self._priority = {row["name"]: int(row["priority"]) for row in rows}

    def is_enabled(self, name: str, default: bool = True) -> bool:
        return self._enabled.get(name, default)

    def priority(self, name: str, default: int) -> int:
        return self._priority.get(name, default)

    def has_override(self, name: str) -> bool:
        return name in self._enabled or name in self._priority


#: Process-wide metrics read by the panel and written by the router.
ai_metrics = AIMetrics()

#: Process-wide routing policy written by the panel and read by the router.
provider_policy = ProviderPolicy()
