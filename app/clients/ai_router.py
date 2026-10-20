"""Multi-provider AI router with priority fallback and circuit breakers.

Production AI traffic flows through this router.  It holds the configured
provider fleet (each an :class:`OpenAICompatibleClient` with its own pacer,
semaphore and circuit breaker), selects a model per request from the
provider's ``models`` mapping, picks the first healthy provider by
priority, and falls back to the next one on any provider failure (429,
5xx, timeout, auth rejection, unparsable body).  The user's daily quota is
checked before any provider is touched and charged after a successful
answer, and the per-user safety filter pipeline wraps the request/response
content.

The engine is exposed two ways:

* :class:`AIRouter.complete` — the full routing path used by HTTP routes.
* :func:`run_ai_request` — a single entry point services call.  When handed
  an :class:`AIRouter` it runs the full path; when handed a legacy
  ``AIClient`` duck-type it calls ``create_chat_completion`` /
  ``create_chat_completion_with_history`` directly, so existing fakes and
  thin clients keep working unchanged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.cache.redis_client import RedisClient
from app.clients.ai_client import OpenAICompatibleClient, _GroqPacer
from app.clients.ai_metrics import ai_metrics, provider_policy
from app.clients.ai_quota import AIQuotaTracker, estimate_tokens
from app.clients.ai_types import AIRequest, AIResponse
from app.clients.prompt_safety import with_safety_preamble
from app.clients.safety import DEFAULT_SAFETY_PIPELINE, SafetyFilter
from app.core.config import AIProviderConfig, Settings
from app.core.logging import get_logger
from app.exceptions.ai_errors import AIErrorCode, AIUpstreamError

logger = get_logger(__name__)


class CircuitBreaker:
    """Trips a provider after N consecutive failures, then opens for a cooldown.

    While open, the router skips the provider entirely, protecting it (and
    the request latency budget) from a provider that just started failing.
    After ``cooldown_seconds`` the breaker half-reopens on the next probe
    and a single success resets it.
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_seconds = max(0.0, cooldown_seconds)
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def _maybe_recover(self) -> None:
        """Close a breaker whose cooldown has expired (lazy recovery)."""
        if (
            self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._cooldown_seconds
        ):
            self._opened_at = None
            self._consecutive_failures = 0

    def is_open(self) -> bool:
        """True when the provider must currently be skipped."""
        self._maybe_recover()
        return self._opened_at is not None

    def record_failure(self) -> None:
        """Count one failure; tripping the breaker when the threshold is hit."""
        if self.is_open():
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = time.monotonic()

    def record_success(self) -> None:
        """Reset the failure streak (and close the breaker)."""
        self._consecutive_failures = 0
        self._opened_at = None


class ModelSelector:
    """Pick the model for a request from a provider's ``models`` mapping.

    Keys are tried most-specific first: ``"<tier>:<task>"``, ``"<tier>"``,
    ``"<task>"``, ``"default"``.  The first hit wins; a provider with no
    mapping uses its ``default_model``.
    """

    def select(self, config: AIProviderConfig, request: AIRequest) -> str:
        candidates = (
            f"{request.plan_tier.value}:{request.task_type.value}",
            request.plan_tier.value,
            request.task_type.value,
            "default",
        )
        for key in candidates:
            model = config.models.get(key)
            if model:
                return model
        return config.default_model


@dataclass
class ProviderRoute:
    """One provider's client, breaker and label inside the router."""

    config: AIProviderConfig
    client: OpenAICompatibleClient
    breaker: CircuitBreaker


def default_openai_client(config: AIProviderConfig) -> OpenAICompatibleClient:
    """Build a configured provider client with per-provider pacing.

    Every provider gets its own sliding-window pacer and concurrency
    semaphore from its own ``rpm``/``tpm``/``max_concurrency`` settings, so
    slots on one provider are never stolen by traffic on another.
    """
    return OpenAICompatibleClient(
        base_url=config.base_url,
        api_key=config.api_key or "",
        model=config.default_model,
        max_output_tokens=config.max_output_tokens,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        provider_name=config.name,
        pacer=_GroqPacer(rpm=config.rpm, tpm=config.tpm),
        semaphore=asyncio.Semaphore(config.max_concurrency),
    )


class AIRouter:
    """Routes AI requests to the best available provider with fallback.

    Args:
        providers: The configured fleet (already filtered to enabled
            providers with an API key).  ``priority`` decides order.
        quota_tracker: The per-user daily quota backend.
        settings: Global settings (breaker defaults).
        safety: Pre/post content filter pipeline; defaults to pass-through.
        breaker_failure_threshold / breaker_cooldown_seconds: Overrides for
            the per-provider circuit breakers.
        client_factory: Builds the provider client for a config.  Defaults
            to :func:`default_openai_client`; tests inject fakes here.
    """

    def __init__(
        self,
        *,
        providers: list[AIProviderConfig],
        quota_tracker: AIQuotaTracker,
        settings: Settings,
        safety: SafetyFilter | None = None,
        breaker_failure_threshold: int | None = None,
        breaker_cooldown_seconds: float | None = None,
        client_factory: Callable[[AIProviderConfig], OpenAICompatibleClient] | None = None,
    ) -> None:
        names = {p.name for p in providers}
        if len(names) != len(providers):
            raise ValueError("AI provider names must be unique")
        factory = client_factory or default_openai_client
        threshold = (
            breaker_failure_threshold
            if breaker_failure_threshold is not None
            else settings.ai_breaker_failure_threshold
        )
        cooldown = (
            breaker_cooldown_seconds
            if breaker_cooldown_seconds is not None
            else settings.ai_breaker_cooldown_seconds
        )
        self._routes: list[ProviderRoute] = [
            ProviderRoute(
                config=p,
                client=factory(p),
                breaker=CircuitBreaker(failure_threshold=threshold, cooldown_seconds=cooldown),
            )
            for p in sorted(providers, key=lambda c: c.priority)
        ]
        self._model_selector = ModelSelector()
        self._quota_tracker = quota_tracker
        self._safety = safety or DEFAULT_SAFETY_PIPELINE

    @property
    def providers(self) -> list[str]:
        """Provider names in routing (priority) order."""
        return [r.config.name for r in self._routes]

    @property
    def provider_count(self) -> int:
        """Number of providers in the fleet (0 means misconfigured)."""
        return len(self._routes)

    def breaker_states(self) -> dict[str, bool]:
        """Return each provider's circuit-breaker state (``True`` = open).

        Used by the admin "Operações de IA" page to show which provider is
        currently being skipped.
        """
        return {route.config.name: route.breaker.is_open() for route in self._routes}

    async def complete(self, request: AIRequest) -> AIResponse:
        """Route one request, returning the first successful provider answer.

        The user's daily quota is checked before any provider is touched and
        is charged only after a success, so a lost request does not burn
        today's budget.  On success the provider's breaker is reset; on
        failure it is counted and the request moves to the next provider.

        Raises:
            AIQuotaExceeded: The user's daily budget is already spent (no
                provider is contacted, no fallback).
            AIUpstreamError(502 ``ai_all_providers_failed``): Every provider
                failed, is breaker-open, or the fleet is empty.
        """
        if not self._routes:
            logger.error("ai_router_no_providers", extra={"request_task": request.task_type.value})
            raise AIUpstreamError(AIErrorCode.ALL_PROVIDERS_FAILED)

        if request.user_id is not None:
            await self._quota_tracker.ensure_within_limits(request.user_id, request.plan_tier)

        user_message = await self._safety.before_request(request.user_message)

        fallback_reasons: list[tuple[str, str]] = []
        for route in self._ordered_routes():
            name = route.config.name
            if not provider_policy.is_enabled(name, route.config.enabled):
                fallback_reasons.append((name, "disabled"))
                logger.info("ai_provider_skipped", extra={"provider": name, "reason": "disabled"})
                continue
            if route.breaker.is_open():
                ai_metrics.provider(name).record_skip()
                fallback_reasons.append((name, "breaker_open"))
                logger.warning(
                    "ai_provider_skipped",
                    extra={"provider": name, "reason": "breaker_open"},
                )
                continue
            try:
                response = await self._call_provider(route, request, user_message)
            except AIUpstreamError as exc:
                route.breaker.record_failure()
                ai_metrics.provider(name).record_failure(exc.code.value)
                ai_metrics.provider(name).record_fallback(exc.code.value)
                fallback_reasons.append((name, exc.code.value))
                logger.warning(
                    "ai_provider_failed_fallback",
                    extra={
                        "provider": name,
                        "error_code": exc.code.value,
                        "fallback_to": self._next_provider_name(name),
                    },
                )
                continue
            except Exception:
                route.breaker.record_failure()
                ai_metrics.provider(name).record_failure("unexpected_error")
                ai_metrics.provider(name).record_fallback("unexpected_error")
                logger.exception("ai_provider_unexpected_error", extra={"provider": name})
                fallback_reasons.append((name, "unexpected_error"))
                continue
            route.breaker.record_success()
            return response

        reason_summary = "; ".join(f"{name}:{reason}" for name, reason in fallback_reasons)
        logger.error(
            "ai_all_providers_failed",
            extra={"provider_attempts": reason_summary, "request_task": request.task_type.value},
        )
        raise AIUpstreamError(AIErrorCode.ALL_PROVIDERS_FAILED)

    def _ordered_routes(self) -> list[ProviderRoute]:
        """Return the routes ordered by the effective (policy-aware) priority."""
        return sorted(
            self._routes,
            key=lambda route: provider_policy.priority(route.config.name, route.config.priority),
        )

    async def _call_provider(
        self, route: ProviderRoute, request: AIRequest, user_message: str
    ) -> AIResponse:
        """Run the request through one provider, applying safety + quota.

        Logs provider, model, latency and (estimated) tokens — never the
        message content.
        """
        model = self._model_selector.select(route.config, request)
        client = route.client
        # The platform-owned safety preamble is prepended here, after the
        # admin-authored prompt has been loaded, so no admin prompt can remove
        # or override it.  Idempotent, so a prompt that already carries it is
        # left untouched.
        system_prompt = with_safety_preamble(request.system_prompt)
        started = time.monotonic()
        if request.history:
            text = await client.create_chat_completion_with_history(
                system_prompt, request.history, model=model
            )
        else:
            text = await client.create_chat_completion(
                system_prompt, user_message, model=model
            )
        text = await self._safety.after_response(text)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        ai_metrics.provider(route.config.name).record_success(elapsed_ms)

        if request.user_id is not None:
            estimated = estimate_tokens(
                system_prompt=system_prompt,
                user_message=user_message,
                max_output_tokens=route.config.max_output_tokens,
            )
            await self._quota_tracker.record_usage(
                request.user_id, tokens_used=estimated, plan_tier=request.plan_tier
            )

        logger.info(
            "ai_request_served",
            extra={
                "provider": route.config.name,
                "model": model,
                "task_type": request.task_type.value,
                "plan_tier": request.plan_tier.value,
                "elapsed_ms": elapsed_ms,
            },
        )
        return AIResponse(text=text, provider=route.config.name, model=model)

    def _next_provider_name(self, failed_name: str) -> str:
        names = self.providers
        try:
            index = names.index(failed_name)
        except ValueError:
            return ""
        return names[index + 1] if index + 1 < len(names) else ""


def build_ai_router(settings: Settings, cache: RedisClient) -> AIRouter:
    """Assemble the production router from settings and the shared cache."""
    return AIRouter(
        providers=settings.ai_providers,
        quota_tracker=AIQuotaTracker(cache, settings),
        settings=settings,
    )


async def run_ai_request(client: Any, request: AIRequest) -> AIResponse:
    """Serve one request through a router or a legacy single-provider client.

    ``client`` is either the :class:`AIRouter` — in which case routing,
    quota, safety and fallback all run — or any object implementing the
    ``AIClient`` duck-type (``create_chat_completion`` /
    ``create_chat_completion_with_history``), which is called directly.
    The duck-typed path keeps the dozens of existing test fakes working
    unchanged and gives small standalone clients a zero-cost path.
    """
    if isinstance(client, AIRouter):
        return await client.complete(request)
    if request.history:
        text = await client.create_chat_completion_with_history(
            request.system_prompt, request.history
        )
    else:
        text = await client.create_chat_completion(request.system_prompt, request.user_message)
    provider = getattr(client, "provider_name", None) or type(client).__name__
    model = getattr(client, "model", None) or ""
    return AIResponse(text=text, provider=provider, model=model)
