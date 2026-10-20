"""
AI client abstraction and OpenAI-compatible provider implementation.

This module defines a reusable client interface that can be swapped out
for alternative providers without changing the service layer.  Every
supported provider speaks the OpenAI chat-completions protocol, so the
generic :class:`OpenAICompatibleClient` carries the whole request engine
(pacing, retries, error taxonomy, response parsing) and each concrete
provider only supplies its configuration and traffic-control policy.
:class:`GroqAIClient` is the backwards-compatible Groq implementation.

Free-tier traffic control
-------------------------
Groq enforces its rate limits (RPM / RPD / TPM / TPD) **per organization**,
i.e. every API key and worker on the account shares one budget.  For the
default model (``openai/gpt-oss-120b``) the free tier is 30 requests/min,
1K requests/day, 8K tokens/min, 200K tokens/day.  To stay inside that budget
this module:

    * logs every outgoing request (timestamp, correlation id, model, attempt,
      estimated tokens) and reads Groq's ``x-ratelimit-*`` headers so the
      limiting counter is always identifiable;
    * gates all traffic with a process-wide sliding-window pacer (defaults 20
      RPM / 6K TPM, below the org limit) and a concurrency semaphore (default 2),
      so concurrent users queue instead of bursting;
    * does NOT silently retry ``429``s by default (``GROQ_MAX_ATTEMPTS=1``):
      each attempt counts against the org quota and a short backoff cannot
      outlast a per-minute window, so retrying multiplies the volume that
      caused the throttle in the first place;
    * caps each response with ``max_tokens`` so output can never blow the
      token budget unpredictably;
    * bounds the **total** time a request may take.  A call can wait twice —
      once in the pacer queue and once on the network — and an unbounded wait
      is what makes the app hang and eventually show its own timeout.  Both
      waits are capped by :data:`_REQUEST_BUDGET_SECONDS` (60s, the budget
      the mobile app waits for), and giving up is reported as a typed,
      app-mappable error instead of a silent stall.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.exceptions.ai_errors import AIErrorCode, AIUpstreamError

logger = get_logger(__name__)

#: Hard ceiling, in seconds, for one AI request end to end (queue + network).
#: Mirrors ``GROQ_REQUEST_BUDGET_SECONDS`` and is what the mobile app waits
#: for; a request that cannot be served inside it fails fast with a 429/504
#: the app can explain, instead of leaving the user watching a spinner.
_REQUEST_BUDGET_SECONDS = 60.0
#: Safety margin subtracted from the budget so the worst case (queued right
#: at the deadline, then a full-length HTTP timeout, then the response) is
#: still comfortably inside it.
_REQUEST_BUDGET_MARGIN_SECONDS = 5.0
#: ``Retry-After`` (seconds) advertised when *our own* queue is the reason a
#: request was refused.  Short by design: a pacer slot frees up in seconds.
_GROQ_BUSY_RETRY_AFTER_SECONDS = 5
#: ``Retry-After`` fallback for a Groq 429 with no usable header.  Groq's
#: windows are per-minute, so a minute is the honest hint.
_GROQ_RATE_LIMIT_RETRY_AFTER_SECONDS = 60
#: TCP/TLS connection attempts are cheap and rarely hang, so they get a much
#: smaller slice of the budget than the response read.
_GROQ_CONNECT_TIMEOUT_SECONDS = 10.0
#: Smallest wait granted to a pacing queue before it is declared busy.  Only
#: relevant once the queue budget is already spent: ``asyncio.wait_for`` treats
#: ``timeout=0`` as "cancel immediately", which would report ``ai_busy`` for a
#: slot that was free all along.  Far below any meaningful delay.
_MIN_SLOT_WAIT_SECONDS = 0.01

#: Exponential-backoff base (seconds) used when neither ``Retry-After`` nor
#: any other hint is present: 1s, then 2s.
_GROQ_BASE_BACKOFF_SECONDS = 1.0
#: Upper bound for a single backoff wait, so a request never lingers too long.
_GROQ_MAX_BACKOFF_SECONDS = 4.0
#: Random jitter added to the backoff to avoid a thundering herd when several
#: requests are throttled at once.
_GROQ_JITTER_SECONDS = 0.5
#: How often the rate pacer re-checks the sliding window while throttled.
_GROQ_PACER_POLL_SECONDS = 0.25
#: Characters treated as a single token when estimating payload size.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Module-level reusable HTTP client
# ---------------------------------------------------------------------------
# A single :class:`httpx.AsyncClient` is created at module level so that
# every AI request reuses the same connection pool.  This avoids the
# overhead of creating a new TCP connection for each request and limits
# the number of simultaneous connections to the AI provider.
_httpx_client: httpx.AsyncClient | None = None
_httpx_initialized: bool = False


def _get_httpx_client() -> httpx.AsyncClient:
    """Return a module-level :class:`httpx.AsyncClient`, initializing it once.

    The timeout is derived from the request budget (:func:`request_budget`)
    so the network wait and the queue wait are two slices of the same 60s
    ceiling.  The budget depends only on settings, so it is resolved once
    here instead of on every call.
    """
    global _httpx_client, _httpx_initialized
    if not _httpx_initialized:
        _queue_wait, http_timeout = request_budget()
        _httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(http_timeout, connect=min(http_timeout, _GROQ_CONNECT_TIMEOUT_SECONDS))
        )
        _httpx_initialized = True
    assert _httpx_client is not None
    return _httpx_client


async def close_http_client() -> None:
    """Close the module-level HTTP client, releasing its connection pool.

    Called from the application lifespan shutdown so the process can exit
    cleanly without ``asyncio`` "cancelled / unclosed event loop" noise.
    A subsequent :func:`_get_httpx_client` call re-creates the client.
    """
    global _httpx_client, _httpx_initialized
    if _httpx_client is not None:
        await _httpx_client.aclose()
        _httpx_client = None
        _httpx_initialized = False


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into a delay in seconds.

    Handles both allowed formats: an integer number of seconds and an
    HTTP-date.  Returns ``None`` when the header is absent or unparsable.
    """
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return float(stripped)
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max((parsed - datetime.now(UTC)).total_seconds(), 0.0)


def _estimate_payload_tokens(payload: dict[str, Any], max_output_tokens: int) -> int:
    """Approximate the tokens (input + capped output) a request will consume.

    Used by the rate pacer to keep the process inside Groq's per-minute
    token budget.  Input tokens are estimated as ``chars / 4`` plus a small
    per-message overhead; the reserved output is the configured
    ``max_tokens`` cap, which bounds the worst case before it happens.
    """
    chars = 0
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content", "")
            if content is not None:
                chars += len(str(content))
    input_tokens = chars // _CHARS_PER_TOKEN + 4 * (len(messages) if isinstance(messages, list) else 0)
    return input_tokens + max(0, max_output_tokens)


def _response_error_message(response: httpx.Response) -> str | None:
    """Extract Groq's ``error.message`` from a response body, if present."""
    try:
        data = response.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return None


def _rate_limit_headers(response: httpx.Response) -> dict[str, Any]:
    """Snapshot Groq's standard ``x-ratelimit-*`` header values.

    Groq always returns these headers (not just on 429): the
    ``-requests`` pair refers to the daily request limit (RPD), the
    ``-tokens`` pair refers to the per-minute token limit (TPM), and
    ``retry-after`` is present only when a 429 was actually returned.
    Knowing which counter is exhausted is what tells us whether we are
    being throttled on RPM, RPD, TPM or TPD — and ``x-ratelimit-reset-*``
    tells us how long the window is.
    """
    headers: Mapping[str, str] = response.headers or {}
    return {
        "retry_after": headers.get("retry-after"),
        "x_ratelimit_limit_requests": headers.get("x-ratelimit-limit-requests"),
        "x_ratelimit_remaining_requests": headers.get("x-ratelimit-remaining-requests"),
        "x_ratelimit_reset_requests": headers.get("x-ratelimit-reset-requests"),
        "x_ratelimit_limit_tokens": headers.get("x-ratelimit-limit-tokens"),
        "x_ratelimit_remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
        "x_ratelimit_reset_tokens": headers.get("x-ratelimit-reset-tokens"),
    }


# ---------------------------------------------------------------------------
# Process-wide Groq traffic control
# ---------------------------------------------------------------------------
# The Groq free tier enforces its RPM/TPM/RPD/TPD budget **per organization**,
# shared by every API key and worker.  Whoever talks to Groq through this
# process must therefore share one pacer and one semaphore regardless of how
# many ``GroqAIClient`` instances the routers create, or concurrent users would
# burst right past the quota with nothing stopping them.


class _GroqPacer:
    """Sliding-window rate gate shared by every Groq request in this process.

    Enforces both a requests-per-minute ceiling and a tokens-per-minute
    ceiling (defaults 20 RPM / 6K TPM, i.e. margin below Groq's free-tier
    org budget of 30 RPM / 8K TPM).  :meth:`acquire` blocks the caller until
    a slot and enough token budget are available in the current window, so
    bursts are *queued* (spread out) instead of firing all at once.
    """

    def __init__(self, rpm: int, tpm: int, window_seconds: float = 60.0) -> None:
        self._rpm = max(1, rpm)
        self._tpm = max(1, tpm)
        self._window = window_seconds
        self._request_times: deque[float] = deque()
        self._token_uses: deque[tuple[float, float]] = deque()

    async def acquire(self, tokens: float = 0.0) -> None:
        """Block until the app may send one request consuming ``tokens``."""
        while True:
            now = time.monotonic()
            while self._request_times and now - self._request_times[0] > self._window:
                self._request_times.popleft()
            while self._token_uses and now - self._token_uses[0][0] > self._window:
                self._token_uses.popleft()
            window_tokens = sum(used for _, used in self._token_uses)
            if len(self._request_times) + 1 <= self._rpm and window_tokens + tokens <= self._tpm:
                self._request_times.append(now)
                self._token_uses.append((now, tokens))
                return
            await asyncio.sleep(_GROQ_PACER_POLL_SECONDS)

    def _queued(self) -> int:
        """Number of requests recorded in the current window (tests/debug)."""
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] > self._window:
            self._request_times.popleft()
        return len(self._request_times)


_pacer: _GroqPacer | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_groq_pacer() -> _GroqPacer:
    """Return the process-wide :class:`_GroqPacer`, created from settings once."""
    global _pacer
    if _pacer is None:
        settings = get_settings()
        _pacer = _GroqPacer(settings.groq_rate_limit_rpm, settings.groq_token_budget_per_minute)
    return _pacer


def _get_groq_semaphore() -> asyncio.Semaphore:
    """Return the process-wide concurrency limiter for Groq requests."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(get_settings().groq_max_concurrency)
    return _semaphore


def request_budget() -> tuple[float, float]:
    """Return ``(queue_wait_seconds, http_timeout_seconds)`` for one call.

    The two waits are derived from a single budget so they can never add up
    to more than :data:`_REQUEST_BUDGET_SECONDS`: whatever is left for the
    network call after reserving the configured queueing allowance is the
    effective HTTP timeout.  A configured ``GROQ_API_TIMEOUT_SECONDS`` that
    does not fit is clamped instead of silently pushing the request past the
    budget, and the clamp is logged once per process so a misconfiguration is
    visible.
    """
    settings = get_settings()
    budget = min(settings.groq_request_budget_seconds, _REQUEST_BUDGET_SECONDS)
    usable = max(budget - _REQUEST_BUDGET_MARGIN_SECONDS, 1.0)
    queue_wait = max(min(settings.groq_queue_wait_seconds, usable / 2), 0.0)
    http_timeout = max(min(float(settings.groq_api_timeout_seconds), usable - queue_wait), 1.0)
    return queue_wait, http_timeout


# ---------------------------------------------------------------------------
# Abstract AI client interface
# ---------------------------------------------------------------------------


class AIClient(ABC):
    """Abstract AI client interface used by service-layer code."""

    @abstractmethod
    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        """Create a chat completion from the given system prompt and user message."""
        raise NotImplementedError

    @abstractmethod
    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        """Create a chat completion including prior conversation history.

        Args:
            system_prompt: The system instructions for the assistant.
            messages: A list of prior ``{"role": ..., "content": ...}``
                messages (``user``/``assistant``) ending with the current
                user message.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenAI-compatible AI client implementation
# ---------------------------------------------------------------------------
# Every provider behind :class:`AIRouter` (Groq, OpenAI, OpenRouter, …) speaks
# the same chat-completions protocol, so one generic engine can drive them
# all.  The only provider-specific knobs are the URL, credential, model and
# traffic-control policy — everything else (pacing, retries, error taxonomy,
# response parsing) lives in :class:`OpenAICompatibleClient`.


class OpenAICompatibleClient(AIClient):
    """Generic client for any OpenAI-compatible chat-completions endpoint.

    Args:
        base_url: Root of the provider API (no trailing slash).
        api_key: Provider credential.
        model: Default model name for this provider.
        max_output_tokens: ``max_tokens`` cap applied to every request.
        timeout_seconds: Configured network budget (also logged).
        max_attempts: Retries against this provider before the router falls
            back to the next one (default 1: no silent retries).
        provider_name: Stable identifier used in logs (e.g. ``"groq"``).
        pacer/semaphore: Optional per-instance traffic control.  When unset,
            :meth:`_resolve_pacer` / :meth:`_resolve_semaphore` decide what
            to use; the default implementation uses whatever the concretely
            configured provider shares process-wide.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        max_output_tokens: int,
        timeout_seconds: float,
        max_attempts: int = 1,
        provider_name: str = "openai_compatible",
        pacer: _GroqPacer | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_attempts = max(1, max_attempts)
        self.provider_name = provider_name
        self._pacer_override = pacer
        self._semaphore_override = semaphore

    # -- seams so subclasses and tests can swap traffic control & transport --
    def _resolve_pacer(self) -> _GroqPacer | None:
        """Return the pacing gate for this provider (``None`` disables it)."""
        return self._pacer_override

    def _resolve_semaphore(self) -> asyncio.Semaphore | None:
        """Return the concurrency limiter for this provider (``None`` disables)."""
        return self._semaphore_override

    def _resolve_http_client(self) -> httpx.AsyncClient:
        """Return the HTTP transport (the shared module-level pool by default)."""
        return _get_httpx_client()

    @property
    def _log_event_stem(self) -> str:
        """Stable stem for log event names, e.g. ``groq``, ``openai``."""
        return self.provider_name.lower().replace("-", "_")

    @property
    def _log_label(self) -> str:
        """Human-readable provider label used in log messages."""
        return "Groq" if self.provider_name == "groq" else self.provider_name.replace("-", " ").title()

    async def create_chat_completion(
        self, system_prompt: str, user_message: str, *, model: str | None = None
    ) -> str:
        """Send a chat completion request and return the assistant reply.

        Args:
            system_prompt: System instructions for the assistant.
            user_message: The current user turn.
            model: Optional per-request model override (used by the router's
                model selector; defaults to the client's configured model).
        """
        return await self._request(
            self._build_payload(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                model=model,
            )
        )

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> str:
        """Send a chat completion request with prior history.

        The system prompt is prepended to the supplied message history, so
        the model can keep the conversation context across turns.
        """
        return await self._request(
            self._build_payload(
                [{"role": "system", "content": system_prompt}, *messages], model=model
            )
        )

    def _build_payload(
        self, messages: list[dict[str, str]], *, model: str | None = None
    ) -> dict[str, Any]:
        """Build the outgoing request body, always bounding the output length.

        ``max_tokens`` caps a single response so a runaway long reply can
        neither blow the per-minute token budget nor the app's patience.  The
        roadmap JSON and chat replies this app produces fit comfortably.
        """
        payload: dict[str, Any] = {"model": model or self.model, "messages": messages}
        if self.max_output_tokens > 0:
            payload["max_tokens"] = self.max_output_tokens
        return payload

    async def _request(self, payload: dict[str, Any]) -> str:
        """Post a payload to the provider and return the assistant reply.

        Every outgoing request is logged with a timestamp, a correlation id
        and the estimated token cost, so the actual request rate reaching the
        provider is always observable.  Before hitting the network the shared
        in-process pacer and semaphore ensure the process stays inside the
        provider's RPM/TPM budget (with margin) and never bursts.

        On a provider ``429`` the response is *not* retried by default
        (``max_attempts`` defaults to 1): on a free tier every attempt,
        successful or not, counts against the quota and the per-minute window
        cannot be escaped with a short backoff, so retrying multiplies the
        very problem it is meant to solve.  The specific exhausted limit and
        the ``Retry-After`` hint are logged and propagated to the caller.

        Time budget: the call is abandoned once :func:`request_budget` is
        exhausted, so an AI route always answers (with a mappable error)
        inside the budget the mobile app waits for rather than hanging.

        Args:
            payload: The full request body (``model`` + ``messages``).

        Returns:
            The assistant's reply text.

        Raises:
            AIUpstreamError(429 ``ai_busy``): Our own queue could not free a
                slot in time — another user is using the AI right now.
            AIUpstreamError(429 ``ai_rate_limited``): The provider's quota is
                exhausted (with the exact limit and retry hint in the detail
                and headers).
            AIUpstreamError(504 ``ai_provider_timeout``): The provider did not
                answer in time.
            AIUpstreamError(502): The provider is unreachable, rejects our
                API key, errored, or returned a body we cannot use.
        """
        client = self._resolve_http_client()
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        estimated_tokens = _estimate_payload_tokens(payload, self.max_output_tokens)
        pacer = self._resolve_pacer()
        semaphore = self._resolve_semaphore()
        queue_wait, _http_timeout = request_budget()
        # Both queue stages share ONE queue deadline.  Giving the pacer the
        # full ``queue_wait`` and then letting the semaphore consume whatever
        # was left of ``queue_wait + http_timeout`` let a request burn
        # ``queue_wait`` on the pacer, then up to ``queue_wait + http_timeout``
        # more on the semaphore, then a full HTTP timeout on the wire — three
        # times the budget the mobile client is willing to wait.  The pacing
        # waits are a single shared allowance and the HTTP timeout is charged
        # separately, so the ceiling is ``queue_wait + http_timeout`` for every
        # attempt, retries included.
        queue_deadline = time.monotonic() + queue_wait

        last_retry_after: float | None = None
        for attempt in range(1, self.max_attempts + 1):
            # Wait for an RPM + token slot in the shared window, then take a
            # concurrency slot for the actual network I/O.  Waiting on the
            # pacer happens *before* acquiring the semaphore so a throttled
            # request does not hold a concurrency slot while sleeping.
            if pacer is not None:
                await self._acquire_slot(
                    pacer.acquire(estimated_tokens),
                    url,
                    queue_deadline - time.monotonic(),
                    "rate pacer",
                )
            request_id = uuid4().hex[:8]
            started = time.monotonic()
            logger.info(
                f"{self._log_event_stem}_request_start",
                extra={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "provider": self.provider_name,
                    "model": self.model,
                    "attempt": attempt,
                    "max_attempts": self.max_attempts,
                    "estimated_tokens": estimated_tokens,
                    "message_count": len(payload.get("messages", [])),
                    "pacer_requests_in_window": pacer._queued() if pacer is not None else 0,
                },
            )
            remaining = queue_deadline - time.monotonic()
            if semaphore is not None:
                await self._acquire_slot(
                    semaphore.acquire(),
                    url,
                    max(remaining, 0.0),
                    "concurrency limiter",
                    retry_after=_GROQ_BUSY_RETRY_AFTER_SECONDS,
                )
            semaphore_held = semaphore is not None
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                logger.exception(
                    f"{self._log_label} AI request timed out",
                    extra={"url": url, "request_id": request_id},
                )
                raise AIUpstreamError(AIErrorCode.TIMEOUT) from exc
            except httpx.HTTPError as exc:
                logger.exception(
                    f"{self._log_label} AI request failed",
                    extra={"url": url, "request_id": request_id},
                )
                raise AIUpstreamError(AIErrorCode.PROVIDER_ERROR) from exc
            finally:
                if semaphore_held and semaphore is not None:
                    semaphore.release()

            elapsed_ms = round((time.monotonic() - started) * 1000, 1)

            if response.status_code == 429:
                rate_headers = _rate_limit_headers(response)
                error_message = _response_error_message(response)
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                last_retry_after = retry_after if retry_after is not None else last_retry_after
                logger.warning(
                    f"{self._log_event_stem}_429_rate_limited",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "request_id": request_id,
                        "provider": self.provider_name,
                        "model": self.model,
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "elapsed_ms": elapsed_ms,
                        "retry_after": retry_after,
                        "provider_error_message": error_message,
                        **rate_headers,
                    },
                )
                if attempt < self.max_attempts:
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _GROQ_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    )
                    delay = min(delay, _GROQ_MAX_BACKOFF_SECONDS) + random.uniform(0, _GROQ_JITTER_SECONDS)
                    logger.warning(
                        f"{self._log_label} AI rate limit hit; backing off and retrying",
                        extra={"request_id": request_id, "attempt": attempt, "delay_seconds": round(delay, 2)},
                    )
                    await asyncio.sleep(delay)
                    continue
                raise AIUpstreamError(
                    AIErrorCode.RATE_LIMITED,
                    headers={"Retry-After": str(int(round(last_retry_after or _GROQ_RATE_LIMIT_RETRY_AFTER_SECONDS)))},
                    provider_message=error_message,
                )

            logger.info(
                f"{self._log_event_stem}_request_end",
                extra={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "provider": self.provider_name,
                    "model": self.model,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    **_rate_limit_headers(response),
                },
            )

            if response.status_code in (401, 403):
                # 403 as well as 401: providers answer 403 once a key is
                # revoked or deactivated.  Both mean "this credential will
                # never work", which is an operator problem, not a transient
                # outage.
                logger.warning(
                    f"{self._log_label} AI authentication failed",
                    extra={"request_id": request_id, "status_code": response.status_code},
                )
                raise AIUpstreamError(AIErrorCode.PROVIDER_AUTH)

            if response.is_error:
                logger.error(
                    f"{self._log_label} AI returned an error response",
                    extra={"status_code": response.status_code, "body": response.text},
                )
                raise AIUpstreamError(
                    AIErrorCode.PROVIDER_ERROR,
                    provider_message=_response_error_message(response),
                )

            try:
                data = response.json()
            except ValueError as exc:
                logger.exception(
                    f"{self._log_label} AI returned invalid JSON",
                    extra={"body": response.text},
                )
                raise AIUpstreamError(AIErrorCode.INVALID_RESPONSE) from exc

            if not isinstance(data, dict):
                # A 200 can still carry a JSON array, string or number (a
                # captive-portal page, a proxy error).  Calling ``.get`` on it
                # would raise AttributeError and surface as an unexplained 500
                # instead of a mappable ``ai_invalid_response``.
                logger.error(
                    f"{self._log_label} AI response is not a JSON object",
                    extra={"response_type": type(data).__name__, "body": response.text},
                )
                raise AIUpstreamError(AIErrorCode.INVALID_RESPONSE)

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                logger.error(f"{self._log_label} AI response missing choices", extra={"body": data})
                raise AIUpstreamError(AIErrorCode.INVALID_RESPONSE)

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                logger.error(f"{self._log_label} AI response choice is invalid", extra={"choice": first_choice})
                raise AIUpstreamError(AIErrorCode.INVALID_RESPONSE)

            message = first_choice.get("message") or first_choice.get("text")
            assistant_text = message.get("content") if isinstance(message, dict) else message

            if not isinstance(assistant_text, str) or not assistant_text.strip():
                # ``message`` is a reserved LogRecord attribute: passing it
                # through ``extra`` makes ``logging`` raise ``KeyError`` from
                # inside the logger call, which would mask this error and turn
                # a mappable 502 into an unexplained 500.
                logger.error(
                    f"{self._log_label} AI response missing assistant text",
                    extra={"assistant_message": message, "body": data},
                )
                raise AIUpstreamError(AIErrorCode.INVALID_RESPONSE)

            return assistant_text.strip()

        # Unreachable: every path above either returns or raises.
        raise AIUpstreamError(AIErrorCode.PROVIDER_ERROR)

    @staticmethod
    async def _acquire_slot(
        awaitable: Awaitable[object],
        url: str,
        timeout_seconds: float,
        what: str,
        *,
        retry_after: int = _GROQ_BUSY_RETRY_AFTER_SECONDS,
    ) -> None:
        """Await a queue slot, giving up as a 429 when the wait runs out.

        ``pacer.acquire``/``semaphore.acquire`` block until a slot frees up.
        Without a bound, a burst of users would keep an AI request waiting
        for the whole 60s window and the app would show its own timeout
        instead of a message it can explain.  Failing fast with
        ``ai_busy`` instead keeps the request inside its budget and tells the
        caller the retry is worth it in seconds, not minutes.
        """
        try:
            # The floor keeps an already-expired budget from failing a wait that
            # could have been satisfied instantly: ``asyncio.wait_for`` with
            # ``timeout=0`` cancels the awaitable before it gets a chance to
            # complete, so a request that had just enough budget left would be
            # reported as busy for no reason.
            await asyncio.wait_for(awaitable, timeout=max(timeout_seconds, _MIN_SLOT_WAIT_SECONDS))
        except TimeoutError:
            logger.warning(
                "AI request gave up waiting for a %s slot",
                what,
                extra={"url": url, "waited_seconds": round(timeout_seconds, 2)},
            )
            raise AIUpstreamError(
                AIErrorCode.BUSY,
                headers={"Retry-After": str(retry_after)},
            ) from None


class GroqAIClient(OpenAICompatibleClient):
    """AI client implementation for Groq Cloud's chat completions endpoint.

    Groq shares its rate budget **per organization** across every API key and
    worker, so all ``GroqAIClient`` instances in this process route their
    traffic through the single module-level pacer and semaphore (see
    ``_get_groq_pacer`` / ``_get_groq_semaphore``).
    """

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY must be set to use the Groq AI client")
        super().__init__(
            base_url=settings.groq_api_base_url,
            api_key=settings.groq_api_key,
            model=settings.groq_api_model,
            max_output_tokens=settings.groq_max_output_tokens,
            timeout_seconds=settings.groq_api_timeout_seconds,
            max_attempts=settings.groq_max_attempts,
            provider_name="groq",
        )

    def _resolve_pacer(self) -> _GroqPacer:
        return _get_groq_pacer()

    def _resolve_semaphore(self) -> asyncio.Semaphore:
        return _get_groq_semaphore()
