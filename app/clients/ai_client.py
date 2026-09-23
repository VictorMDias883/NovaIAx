"""
AI client abstraction and Groq Cloud implementation.

This module defines a reusable client interface that can be swapped out
for alternative providers without changing the service layer.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Maximum number of attempts (including the initial request) made before a
#: Groq ``429`` rate-limit response is surfaced to the caller.
_GROQ_MAX_ATTEMPTS = 3
#: Exponential-backoff base (seconds) used when neither ``Retry-After`` nor
#: any other hint is present: 1s, then 2s.
_GROQ_BASE_BACKOFF_SECONDS = 1.0
#: Upper bound for a single backoff wait, so a request never lingers too long.
_GROQ_MAX_BACKOFF_SECONDS = 4.0
#: Random jitter added to the backoff to avoid a thundering herd when several
#: requests are throttled at once.
_GROQ_JITTER_SECONDS = 0.5


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

        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY must be set to use the Groq AI client")

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        """Send a chat completion request to Groq Cloud and return the assistant reply."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        return await self._request(payload)

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        """Send a chat completion request with prior history to Groq Cloud.

        The system prompt is prepended to the supplied message history, so
        the model can keep the conversation context across turns.
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
        }
        return await self._request(payload)

    async def _request(self, payload: dict[str, Any]) -> str:
        """Post a payload to Groq Cloud and return the assistant reply.

        A Groq ``429`` (rate limited) response is retried a small number of
        times with exponential backoff, honoring the ``Retry-After`` header
        when Groq supplies one, before the failure is surfaced as a ``429``.
        Transient timeouts and network errors are not retried and surface as
        ``504``/``502`` like before.

        Args:
            payload: The full request body (``model`` + ``messages``).

        Returns:
            The assistant's reply text.

        Raises:
            HTTPException(429): If Groq keeps returning rate-limit errors.
            HTTPException(502/504): If the AI provider is unreachable,
                fails authentication, or returns an invalid response.
        """
        client = _get_httpx_client()
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_retry_after: float | None = None
        for attempt in range(1, _GROQ_MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                logger.exception("Groq AI request timed out", extra={"url": url})
                raise HTTPException(status_code=504, detail="AI provider request timed out") from exc
            except httpx.HTTPError as exc:
                logger.exception("Groq AI request failed", extra={"url": url})
                raise HTTPException(status_code=502, detail="Failed to communicate with AI provider") from exc

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                last_retry_after = retry_after
                if attempt < _GROQ_MAX_ATTEMPTS:
                    delay = (
                        retry_after
                        if retry_after is not None
                        else _GROQ_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    )
                    delay = min(delay, _GROQ_MAX_BACKOFF_SECONDS) + random.uniform(0, _GROQ_JITTER_SECONDS)
                    logger.warning(
                        "Groq AI rate limit hit; backing off and retrying",
                        extra={
                            "attempt": attempt,
                            "max_attempts": _GROQ_MAX_ATTEMPTS,
                            "retry_after": retry_after,
                            "delay_seconds": round(delay, 2),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Groq AI rate limit exceeded after retries",
                    extra={"status_code": response.status_code, "body": response.text},
                )
                raise HTTPException(
                    status_code=429,
                    detail="AI provider rate limit exceeded",
                    headers={"Retry-After": str(int(round(last_retry_after or 60)))},
                )

            if response.status_code == 401:
                logger.warning("Groq AI authentication failed", extra={"status_code": response.status_code})
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
