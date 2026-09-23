"""
AI client abstraction and Groq Cloud implementation.

This module defines a reusable client interface that can be swapped out
for alternative providers without changing the service layer.

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
      token budget unpredictably.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

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
    """Return a module-level :class:`httpx.AsyncClient`, initializing it once."""
    global _httpx_client, _httpx_initialized
    if not _httpx_initialized:
        settings = get_settings()
        timeout = httpx.Timeout(settings.groq_api_timeout_seconds, connect=settings.groq_api_timeout_seconds)
        _httpx_client = httpx.AsyncClient(timeout=timeout)
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
# Groq Cloud AI client implementation
# ---------------------------------------------------------------------------


class GroqAIClient(AIClient):
    """AI client implementation for Groq Cloud's chat completions endpoint."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.api_key = self.settings.groq_api_key
        self.base_url = self.settings.groq_api_base_url.rstrip("/")
        self.model = self.settings.groq_api_model
        self.timeout_seconds = self.settings.groq_api_timeout_seconds
        self.max_output_tokens = self.settings.groq_max_output_tokens
        self.max_attempts = max(1, self.settings.groq_max_attempts)

        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY must be set to use the Groq AI client")

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        """Send a chat completion request to Groq Cloud and return the assistant reply."""
        return await self._request(
            self._build_payload(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ]
            )
        )

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a chat completion request with prior history to Groq Cloud.

        The system prompt is prepended to the supplied message history, so
        the model can keep the conversation context across turns.
        """
        return await self._request(
            self._build_payload([{"role": "system", "content": system_prompt}, *messages])
        )

    def _build_payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Build the outgoing request body, always bounding the output length.

        ``max_tokens`` caps a single response so a runaway long reply can
        neither blow the per-minute token budget nor the app's patience.  The
        roadmap JSON and chat replies this app produces fit comfortably.
        """
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.max_output_tokens > 0:
            payload["max_tokens"] = self.max_output_tokens
        return payload

    async def _request(self, payload: dict[str, Any]) -> str:
        """Post a payload to Groq Cloud and return the assistant reply.

        Every outgoing request is logged with a timestamp, a correlation id
        and the estimated token cost, so the actual request rate the app is
        sending to Groq is always observable.  Before hitting the network the
        shared in-process pacer and semaphore ensure the process stays inside
        Groq's organization-wide RPM/TPM budget (with margin) and never bursts.

        On a Groq ``429`` the response is *not* retried by default
        (``GROQ_MAX_ATTEMPTS`` defaults to 1): on the free tier every attempt,
        successful or not, counts against the org quota and the per-minute
        window cannot be escaped with a short backoff, so retrying multiplies
        the very problem it is meant to solve.  The specific exhausted limit
        (RPM/TPM/RPD, from Groq's error body and ``x-ratelimit-*`` headers) is
        logged and propagated to the caller in the ``429`` detail, together
        with a ``Retry-After`` hint.

        Args:
            payload: The full request body (``model`` + ``messages``).

        Returns:
            The assistant's reply text.

        Raises:
            HTTPException(429): If Groq is throttling (with the exact limit
                and retry hint in the detail/headers).
            HTTPException(502/504): If the AI provider is unreachable,
                fails authentication, or returns an invalid response.
        """
        client = _get_httpx_client()
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        estimated_tokens = _estimate_payload_tokens(payload, self.max_output_tokens)
        pacer = _get_groq_pacer()
        semaphore = _get_groq_semaphore()

        last_retry_after: float | None = None
        for attempt in range(1, self.max_attempts + 1):
            # Wait for an RPM + token slot in the shared window, then take a
            # concurrency slot for the actual network I/O.  Waiting on the
            # pacer happens *before* acquiring the semaphore so a throttled
            # request does not hold a concurrency slot while sleeping.
            await pacer.acquire(estimated_tokens)
            request_id = uuid4().hex[:8]
            started = time.monotonic()
            logger.info(
                "groq_request_start",
                extra={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "model": self.model,
                    "attempt": attempt,
                    "max_attempts": self.max_attempts,
                    "estimated_tokens": estimated_tokens,
                    "message_count": len(payload.get("messages", [])),
                    "pacer_requests_in_window": pacer._queued(),
                },
            )
            await semaphore.acquire()
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                logger.exception("Groq AI request timed out", extra={"url": url, "request_id": request_id})
                raise HTTPException(status_code=504, detail="AI provider request timed out") from exc
            except httpx.HTTPError as exc:
                logger.exception("Groq AI request failed", extra={"url": url, "request_id": request_id})
                raise HTTPException(status_code=502, detail="Failed to communicate with AI provider") from exc
            finally:
                semaphore.release()

            elapsed_ms = round((time.monotonic() - started) * 1000, 1)

            if response.status_code == 429:
                rate_headers = _rate_limit_headers(response)
                error_message = _response_error_message(response)
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                last_retry_after = retry_after if retry_after is not None else last_retry_after
                logger.warning(
                    "groq_429_rate_limited",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "request_id": request_id,
                        "model": self.model,
                        "attempt": attempt,
                        "max_attempts": self.max_attempts,
                        "elapsed_ms": elapsed_ms,
                        "retry_after": retry_after,
                        "groq_error_message": error_message,
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
                        "Groq AI rate limit hit; backing off and retrying",
                        extra={"request_id": request_id, "attempt": attempt, "delay_seconds": round(delay, 2)},
                    )
                    await asyncio.sleep(delay)
                    continue
                detail = (
                    f"AI provider rate limit exceeded: {error_message}" if error_message else "AI provider rate limit exceeded"
                )
                raise HTTPException(
                    status_code=429,
                    detail=detail,
                    headers={"Retry-After": str(int(round(last_retry_after or 60)))},
                )

            logger.info(
                "groq_request_end",
                extra={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "model": self.model,
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    **_rate_limit_headers(response),
                },
            )

            if response.status_code == 401:
                logger.warning("Groq AI authentication failed", extra={"request_id": request_id})
                raise HTTPException(status_code=502, detail="AI provider authentication failed")

            if response.is_error:
                logger.error(
                    "Groq AI returned an error response",
                    extra={"status_code": response.status_code, "body": response.text},
                )
                raise HTTPException(status_code=502, detail="AI provider returned an error")

            try:
                data = response.json()
            except ValueError as exc:
                logger.exception("Groq AI returned invalid JSON", extra={"body": response.text})
                raise HTTPException(status_code=502, detail="Invalid response from AI provider") from exc

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                logger.error("Groq AI response missing choices", extra={"body": data})
                raise HTTPException(status_code=502, detail="AI provider returned an unexpected response")

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                logger.error("Groq AI response choice is invalid", extra={"choice": first_choice})
                raise HTTPException(status_code=502, detail="AI provider returned an unexpected response")

            message = first_choice.get("message") or first_choice.get("text")
            if isinstance(message, dict):
                assistant_text = message.get("content")
            else:
                assistant_text = message

            if not isinstance(assistant_text, str) or not assistant_text.strip():
                logger.error("Groq AI response missing assistant text", extra={"message": message, "body": data})
                raise HTTPException(status_code=502, detail="AI provider returned an invalid assistant response")

            return assistant_text.strip()

        # Unreachable: every path above either returns or raises.
        raise HTTPException(status_code=502, detail="AI provider returned an error")
