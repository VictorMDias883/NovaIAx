"""
Tests for the Groq-cloud resilience and AI-rate-limit fixes.

Covers:

1. :class:`GroqAIClient` **does not silently retry** a Groq ``429`` by
   default (``GROQ_MAX_ATTEMPTS=1``): on the free tier every attempt counts
   against the organization quota and a short backoff cannot outlast the
   per-minute window, so the throttle is surfaced immediately with the exact
   limit from Groq's error body and ``x-ratelimit-*`` headers.  Explicit
   opt-in retries still use exponential backoff honoring ``Retry-After``.
   Non-``429`` provider errors are raised immediately.
2. :class:`_GroqPacer` enforces both a requests-per-minute ceiling and a
   tokens-per-minute ceiling shared across the whole process, queueing
   (instead of bursting) traffic that would exceed the org budget.
2. :class:`ConversationCache` trims history by an approximate token budget as
   well as by message count, so conversations neither blow the provider's
   per-minute token quota nor silently drop the most recent turn.
3. The AI rate-limit path helper buckets every endpoint that triggers a Groq
   call (chat, general agent, objective assistant, objective register and
   roadmap renew) under the strict ``rate_limit_ai`` limit.
"""

import asyncio
import json

import pytest
from app.cache.conversation_cache import (
    MAX_HISTORY_MESSAGES,
    MAX_HISTORY_TOKENS,
    ConversationCache,
)
from app.cache.redis_client import RedisClient
from app.clients.ai_client import GroqAIClient, _GroqPacer
from app.core.config import Settings
from fastapi import HTTPException

_SUCCESS_BODY = {"choices": [{"message": {"content": " ok "}}]}


class _FakeResponse:
    def __init__(self, status_code: int, body: object, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _patch_groq_env(monkeypatch: pytest.MonkeyPatch, max_attempts: int = 1) -> None:
    """Give :class:`GroqAIClient` a deterministic key/base URL regardless of ``.env``."""
    settings = Settings(
        groq_api_key="test-key",
        groq_api_base_url="https://api.groq.com/openai/v1",
        groq_max_attempts=max_attempts,
    )
    monkeypatch.setattr("app.clients.ai_client.get_settings", lambda: settings)
    # The pacer/semaphore are process-wide singletons; reset them per test so
    # reservations recorded one test do not throttle the next.
    monkeypatch.setattr(
        "app.clients.ai_client._pacer",
        _GroqPacer(rpm=settings.groq_rate_limit_rpm, tpm=settings.groq_token_budget_per_minute),
    )
    monkeypatch.setattr("app.clients.ai_client._semaphore", asyncio.Semaphore(settings.groq_max_concurrency))


def _make_cache() -> ConversationCache:
    settings = Settings(redis_url="redis://127.0.0.1:1/0")  # unreachable -> memory fallback
    return ConversationCache(RedisClient(settings))


# ---------------------------------------------------------------------------
# Groq 429 handling in the AI client
# ---------------------------------------------------------------------------


def test_429_not_retried_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are OFF by default, so a 429 is surfaced immediately + verbatim."""
    _patch_groq_env(monkeypatch, max_attempts=1)
    sleep_recorder = _SleepRecorder()
    monkeypatch.setattr("app.clients.ai_client.asyncio.sleep", sleep_recorder)

    class _Always429:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, json: object, headers: dict | None) -> _FakeResponse:
            self.calls += 1
            return _FakeResponse(
                429,
                {
                    "error": {
                        "type": "tokens",
                        "message": "on tokens per minute (TPM): Limit 8000, Used 0, Requested ~12903.",
                    }
                },
                {"retry-after": "57", "x-ratelimit-limit-requests": "1000", "x-ratelimit-limit-tokens": "8000"},
            )

    client = _Always429()
    monkeypatch.setattr("app.clients.ai_client._get_httpx_client", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert client.calls == 1, "no silent retries that would multiply the org quota"
    assert exc_info.value.status_code == 429
    assert "on tokens per minute (TPM)" in exc_info.value.detail, "the exact limit must be reported"
    assert exc_info.value.headers == {"Retry-After": "57"}
    assert sleep_recorder.delays == [], "must not sleep-and-retry inside the exhausted window"


def test_retries_once_on_429_honoring_retry_after_when_opting_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """When retries are explicitly enabled, Retry-After is honored once."""
    _patch_groq_env(monkeypatch, max_attempts=3)
    sleep_recorder = _SleepRecorder()
    monkeypatch.setattr("app.clients.ai_client.asyncio.sleep", sleep_recorder)

    class _FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, json: object, headers: dict | None) -> _FakeResponse:
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(429, {"error": {"message": "rate limited"}}, {"retry-after": "2"})
            return _FakeResponse(200, _SUCCESS_BODY)

    flaky = _FlakyClient()
    monkeypatch.setattr("app.clients.ai_client._get_httpx_client", lambda: flaky)

    result = asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert result == "ok"
    assert flaky.calls == 2
    assert len(sleep_recorder.delays) == 1
    assert 2.0 <= sleep_recorder.delays[0] <= 2.5, "the Retry-After delay (plus jitter) must be honored"


def test_exponential_backoff_when_no_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly enabled retries use exponential backoff when no header is given."""
    _patch_groq_env(monkeypatch, max_attempts=3)
    sleep_recorder = _SleepRecorder()
    monkeypatch.setattr("app.clients.ai_client.asyncio.sleep", sleep_recorder)

    class _Two429sThenSuccess:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, json: object, headers: dict | None) -> _FakeResponse:
            self.calls += 1
            if self.calls < 3:
                return _FakeResponse(429, {"error": {"message": "rate limited"}})
            return _FakeResponse(200, _SUCCESS_BODY)

    client = _Two429sThenSuccess()
    monkeypatch.setattr("app.clients.ai_client._get_httpx_client", lambda: client)

    result = asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert result == "ok"
    assert client.calls == 3
    assert len(sleep_recorder.delays) == 2
    # Backoff is base * 2**(attempt-1): 1s then 2s, plus jitter.
    assert 1.0 <= sleep_recorder.delays[0] <= 1.5
    assert 2.0 <= sleep_recorder.delays[1] <= 2.5


def test_exhausted_retries_surface_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once opt-in retries are exhausted the 429 (exact limit + Retry-After) reaches the caller."""
    _patch_groq_env(monkeypatch, max_attempts=3)
    sleep_recorder = _SleepRecorder()
    monkeypatch.setattr("app.clients.ai_client.asyncio.sleep", sleep_recorder)

    class _Always429:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, json: object, headers: dict | None) -> _FakeResponse:
            self.calls += 1
            return _FakeResponse(429, {"error": {"message": "rate limited"}}, {"retry-after": "5"})

    client = _Always429()
    monkeypatch.setattr("app.clients.ai_client._get_httpx_client", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert client.calls == 3
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "AI provider rate limit exceeded: rate limited"
    assert exc_info.value.headers == {"Retry-After": "5"}


def test_non_429_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_groq_env(monkeypatch)
    sleep_recorder = _SleepRecorder()
    monkeypatch.setattr("app.clients.ai_client.asyncio.sleep", sleep_recorder)

    class _Always500:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, json: object, headers: dict | None) -> _FakeResponse:
            self.calls += 1
            return _FakeResponse(500, {"error": {"message": "boom"}})

    client = _Always500()
    monkeypatch.setattr("app.clients.ai_client._get_httpx_client", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert client.calls == 1
    assert exc_info.value.status_code == 502
    assert sleep_recorder.delays == []


# ---------------------------------------------------------------------------
# Process-wide RPM/TPM pacer
# ---------------------------------------------------------------------------


def test_pacer_queues_requests_beyond_the_rpm_ceiling() -> None:
    async def scenario() -> None:
        pacer = _GroqPacer(rpm=2, tpm=100_000, window_seconds=0.05)
        await pacer.acquire(10)
        await pacer.acquire(10)
        assert pacer._queued() == 2

        # Third request must wait for an earlier slot to age out of the window.
        third = asyncio.create_task(pacer.acquire(10))
        await asyncio.sleep(0.02)
        assert not third.done(), "third request must be queued behind the RPM ceiling"
        await asyncio.sleep(0.06)
        await third
        assert pacer._queued() == 1, "only the just-admitted request remains in the window"

    asyncio.run(scenario())


def test_pacer_blocks_requests_exceeding_the_tpm_budget() -> None:
    async def scenario() -> None:
        pacer = _GroqPacer(rpm=100, tpm=100, window_seconds=0.05)
        await pacer.acquire(60)

        second = asyncio.create_task(pacer.acquire(60))
        await asyncio.sleep(0.02)
        assert not second.done(), "120 tokens in the window > 100 TPM must not be admitted"

        # Once the first reservation ages out of the window it is admitted.
        await asyncio.sleep(0.06)
        await second

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Token-budget-aware conversation trimming
# ---------------------------------------------------------------------------


def _estimate(messages: list[dict[str, str]]) -> int:
    return sum(4 + len(message["content"]) // 4 for message in messages)


def test_history_trimmed_to_token_budget() -> None:
    cache = _make_cache()
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 5000}
        for i in range(8)
    ]
    assert _estimate(messages) > MAX_HISTORY_TOKENS

    asyncio.run(cache.save("general_agent", 1, messages))
    stored = asyncio.run(cache.get_messages("general_agent", 1))

    assert _estimate(stored) <= MAX_HISTORY_TOKENS
    assert stored[-1] == messages[-1], "the latest turn must always be preserved"


def test_history_trimmed_to_message_cap_when_short() -> None:
    cache = _make_cache()
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "oi"}
        for i in range(MAX_HISTORY_MESSAGES + 10)
    ]
    assert _estimate(messages) <= MAX_HISTORY_TOKENS

    asyncio.run(cache.save("objective_assistant", 1, messages))
    stored = asyncio.run(cache.get_messages("objective_assistant", 1))

    assert len(stored) <= MAX_HISTORY_MESSAGES
    assert stored[-1] == messages[-1]


def test_huge_single_turn_still_keeps_latest_message() -> None:
    cache = _make_cache()
    messages = [
        {"role": "user", "content": "y" * 20000},
        {"role": "assistant", "content": "y" * 20000},
        {"role": "user", "content": "y" * 20000},
    ]

    asyncio.run(cache.save("general_agent", 1, messages))
    stored = asyncio.run(cache.get_messages("general_agent", 1))

    assert stored[-1] == messages[-1]


def test_small_history_is_not_trimmed() -> None:
    cache = _make_cache()
    messages = [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "tudo bem"}]

    asyncio.run(cache.save("general_agent", 1, messages))
    stored = asyncio.run(cache.get_messages("general_agent", 1))

    assert stored == messages


# ---------------------------------------------------------------------------
# AI rate-limit bucket coverage
# ---------------------------------------------------------------------------


def test_ai_rate_limit_bucket_covers_all_groq_calling_paths() -> None:
    from app.middlewares.rate_limit_middleware import is_ai_rate_limited

    assert is_ai_rate_limited("/api/v1/ai/chat")
    assert is_ai_rate_limited("/api/v1/agents/general/chat")
    assert is_ai_rate_limited("/api/v1/agents/general/conversation")
    assert is_ai_rate_limited("/api/v1/objectives/assistant")
    assert is_ai_rate_limited("/api/v1/objectives/register")
    assert is_ai_rate_limited("/api/v1/objectives/5/roadmap/renew")

    # Non-AI endpoints must keep the default bucket.
    assert not is_ai_rate_limited("/api/v1/objectives/")
    assert not is_ai_rate_limited("/api/v1/objectives/5/roadmap/days")
    assert not is_ai_rate_limited("/api/v1/auth/me")
    assert not is_ai_rate_limited("/health")
    assert not is_ai_rate_limited("/admin/login")

