"""Tests for the AI request budget and the app-mappable error contract.

Covers:

1. **The 60-second ceiling is real** — the pacing wait and the network timeout
   are derived from one budget, and the two pacing stages share a single
   queue allowance.  Giving each stage the full budget let one request wait
   three times as long as the mobile client is willing to.
2. **Every upstream failure maps to a stable code + pt-BR message** with an
   HTTP status the app can act on, and keeps the provider's own diagnostic.
3. **The JSON shape is exactly ``{"detail": ..., "code": ...}``** so the
   mobile client can branch on ``code`` instead of parsing Portuguese text.
4. **``Retry-After`` is sent** when the client should come back later.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app.clients import ai_client
from app.clients.ai_client import (
    _REQUEST_BUDGET_SECONDS,
    GroqAIClient,
    request_budget,
)
from app.core.config import Settings
from app.exceptions.ai_errors import AIErrorCode, AIUpstreamError
from app.exceptions.handlers import register_exception_handlers
from fastapi import FastAPI, HTTPException


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


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    kwargs: dict = {
        "groq_api_key": "test-key",
        "groq_api_base_url": "https://api.groq.com/openai/v1",
        "groq_max_attempts": 1,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    monkeypatch.setattr(ai_client, "get_settings", lambda: settings)
    monkeypatch.setattr(
        ai_client,
        "_pacer",
        ai_client._GroqPacer(rpm=settings.groq_rate_limit_rpm, tpm=settings.groq_token_budget_per_minute),
    )
    monkeypatch.setattr(ai_client, "_semaphore", asyncio.Semaphore(settings.groq_max_concurrency))


def _patch_transport(monkeypatch: pytest.MonkeyPatch, responder) -> None:
    class _Transport:
        async def post(self, url: str, json: object, headers: dict | None):
            return responder(url)

    monkeypatch.setattr(ai_client, "_get_httpx_client", lambda: _Transport())


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(ai_client.asyncio, "sleep", _sleep)


# ---------------------------------------------------------------------------
# 1. The 60-second budget
# ---------------------------------------------------------------------------


def test_default_budget_fits_inside_the_ceiling() -> None:
    queue_wait, http_timeout = request_budget()
    assert queue_wait > 0 and http_timeout > 0
    assert queue_wait + http_timeout <= _REQUEST_BUDGET_SECONDS


def test_budget_slices_never_add_up_to_more_than_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any queue/network configuration must be clamped into the ceiling."""
    _patch_settings(
        monkeypatch,
        groq_request_budget_seconds=600.0,
        groq_queue_wait_seconds=300.0,
        groq_api_timeout_seconds=300.0,
    )
    queue_wait, http_timeout = request_budget()
    assert queue_wait + http_timeout <= _REQUEST_BUDGET_SECONDS
    assert http_timeout < _REQUEST_BUDGET_SECONDS


def test_a_larger_configured_budget_cannot_widen_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, groq_request_budget_seconds=600.0, groq_api_timeout_seconds=300.0)
    queue_wait, http_timeout = request_budget()
    assert queue_wait + http_timeout <= _REQUEST_BUDGET_SECONDS


def test_the_queue_slice_is_capped_at_half_the_usable_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queueing must never starve the network call of its whole budget."""
    _patch_settings(monkeypatch, groq_request_budget_seconds=30.0, groq_queue_wait_seconds=29.0)
    queue_wait, http_timeout = request_budget()
    assert queue_wait <= http_timeout, "queueing may not take more than half the budget"
    assert http_timeout >= 1.0


def test_budget_is_never_negative_or_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, groq_request_budget_seconds=0.0, groq_queue_wait_seconds=0.0)
    queue_wait, http_timeout = request_budget()
    assert queue_wait >= 0.0
    assert http_timeout >= 1.0


def test_the_two_pacing_stages_share_one_queue_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pacer and the semaphore must not each get the full queue budget.

    This is the regression that let a single request wait ``queue_wait``
    (pacer) + ``queue_wait + http_timeout`` (semaphore) + ``http_timeout``
    (wire) — roughly three times the ceiling the app waits for.  The
    pacer really sleeps here so the two measured allowances are real
    remaining-budget values rather than one full budget handed out twice.
    """
    _patch_settings(
        monkeypatch,
        groq_request_budget_seconds=6.0,
        groq_queue_wait_seconds=0.5,
    )
    queue_wait, http_timeout = request_budget()
    _patch_transport(
        monkeypatch,
        lambda _url: _FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]}),
    )

    class _SlowPacer:
        """Consumes most of the queue allowance before letting the request through."""

        async def acquire(self, _tokens: int):
            await asyncio.sleep(queue_wait * 0.8)

        def _queued(self) -> int:
            return 0

    monkeypatch.setattr(ai_client, "_get_groq_pacer", lambda: _SlowPacer())

    waits: list[float] = []
    monkeypatch.setattr(GroqAIClient, "_acquire_slot", _record_wait(waits))

    pacer_sleep = queue_wait * 0.8
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        loop.run_until_complete(GroqAIClient().create_chat_completion("sys", "hi"))
        elapsed = loop.time() - started
    finally:
        loop.close()

    assert len(waits) == 2, "pacer and semaphore must both report their allowance"
    first, second = waits
    assert first == pytest.approx(queue_wait, abs=0.05), "the pacer starts with the full allowance"
    assert second == pytest.approx(queue_wait - pacer_sleep, abs=0.05), (
        "the semaphore must see only what the pacer left behind"
    )
    # The grants chain off one deadline instead of restarting: the time spent
    # queueing can never exceed the single allowance the budget reserved.
    assert elapsed < queue_wait + http_timeout + 0.5
    assert first + second + http_timeout <= _REQUEST_BUDGET_SECONDS


def _record_wait(waits: list[float]):
    async def _acquire_slot(self, awaitable, url, timeout_seconds, what, *, retry_after=None):
        waits.append(timeout_seconds)
        await awaitable

    return _acquire_slot


# ---------------------------------------------------------------------------
# 2. Error taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (AIErrorCode.RATE_LIMITED, 429),
        (AIErrorCode.BUSY, 429),
        (AIErrorCode.TIMEOUT, 504),
        (AIErrorCode.PROVIDER_ERROR, 502),
        (AIErrorCode.PROVIDER_AUTH, 502),
        (AIErrorCode.INVALID_RESPONSE, 502),
    ],
)
def test_every_error_code_has_a_stable_status(code: AIErrorCode, status: int) -> None:
    assert AIUpstreamError(code).status_code == status


def test_every_error_has_a_non_empty_portuguese_message() -> None:
    """The mobile app shows ``detail`` verbatim, so it must never be empty."""
    for code in AIErrorCode:
        message = AIUpstreamError(code).detail
        assert message, f"{code} has no user-facing message"
        assert isinstance(message, str)


def test_error_codes_are_stable_strings() -> None:
    """Renaming a code breaks every deployed client, so they are asserted."""
    assert AIErrorCode.RATE_LIMITED.value == "ai_rate_limited"
    assert AIErrorCode.BUSY.value == "ai_busy"
    assert AIErrorCode.TIMEOUT.value == "ai_provider_timeout"
    assert AIErrorCode.PROVIDER_ERROR.value == "ai_provider_error"
    assert AIErrorCode.PROVIDER_AUTH.value == "ai_provider_auth_failed"
    assert AIErrorCode.INVALID_RESPONSE.value == "ai_invalid_response"


def test_the_provider_diagnostic_is_preserved() -> None:
    """Without this the log says only "something went wrong" and 4am is lost."""
    error = AIUpstreamError(AIErrorCode.PROVIDER_ERROR, provider_message="TPM limit 8000 exceeded")
    assert "TPM limit 8000 exceeded" in error.detail
    assert error.provider_message == "TPM limit 8000 exceeded"


def test_the_default_message_is_used_when_the_provider_is_silent() -> None:
    error = AIUpstreamError(AIErrorCode.PROVIDER_ERROR, provider_message="   ")
    assert "TPM limit 8000 exceeded" not in error.detail
    assert error.detail


def test_custom_detail_replaces_the_default() -> None:
    error = AIUpstreamError(AIErrorCode.RATE_LIMITED, detail="Limite de tokens por minuto esgotado.")
    assert error.detail == "Limite de tokens por minuto esgotado."


# ---------------------------------------------------------------------------
# 3. Provider failures map to the right code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (429, {"error": {"message": "rate limit reached"}}, AIErrorCode.RATE_LIMITED),
        (401, {"error": {"message": "invalid api key"}}, AIErrorCode.PROVIDER_AUTH),
        (403, {"error": {"message": "key revoked"}}, AIErrorCode.PROVIDER_AUTH),
        (500, {"error": {"message": "internal"}}, AIErrorCode.PROVIDER_ERROR),
        (502, {"error": {"message": "bad gateway"}}, AIErrorCode.PROVIDER_ERROR),
        (503, {"error": {"message": "overloaded"}}, AIErrorCode.PROVIDER_ERROR),
    ],
)
def test_provider_status_codes_map_to_codes(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: dict,
    expected: AIErrorCode,
) -> None:
    _patch_settings(monkeypatch)
    _no_sleep(monkeypatch)
    _patch_transport(monkeypatch, lambda _url: _FakeResponse(status_code, body))

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == expected
    assert exc_info.value.detail, "a user-facing message is always required"


def test_an_auth_failure_does_not_leak_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 body can echo the credential; it must not reach the client."""
    _patch_settings(monkeypatch, groq_api_key="sk-super-secret-value")
    _patch_transport(
        monkeypatch,
        lambda _url: _FakeResponse(401, {"error": {"message": "invalid key sk-super-secret-value"}}),
    )

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert "sk-super-secret-value" not in exc_info.value.detail


def test_a_timeout_is_reported_as_a_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)

    class _Hangs:
        async def post(self, url, json, headers):
            raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(ai_client, "_get_httpx_client", lambda: _Hangs())

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.TIMEOUT
    assert exc_info.value.status_code == 504


def test_a_connection_failure_is_reported_as_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)

    class _Refuses:
        async def post(self, url, json, headers):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(ai_client, "_get_httpx_client", lambda: _Refuses())

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.PROVIDER_ERROR
    assert exc_info.value.status_code == 502


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": ""}}]},
    ],
)
def test_an_unusable_body_is_reported_as_an_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
) -> None:
    _patch_settings(monkeypatch)
    _patch_transport(monkeypatch, lambda _url: _FakeResponse(200, body))

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.INVALID_RESPONSE


def test_a_non_json_success_body_is_an_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy error page served with a 200 must not reach the user raw."""

    class _Html:
        async def post(self, url, json, headers):
            class _Response:
                status_code = 200
                is_error = False
                headers: dict = {}
                text = "<html>gateway</html>"

                def json(self):
                    raise ValueError("not json")

            return _Response()

    _patch_settings(monkeypatch)
    monkeypatch.setattr(ai_client, "_get_httpx_client", lambda: _Html())

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("body", [["choices"], "text", 42, None, True])
def test_a_json_body_that_is_not_an_object_is_an_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
) -> None:
    """``.get`` on a JSON array/string crashed with an unexplained 500."""
    _patch_settings(monkeypatch)
    _patch_transport(monkeypatch, lambda _url: _FakeResponse(200, body))

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.INVALID_RESPONSE


def test_an_empty_assistant_message_is_an_invalid_response_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logging the offending message under the key ``message`` raised KeyError
    from inside ``logging``, masking the mappable 502 with a 500."""
    _patch_settings(monkeypatch)
    _patch_transport(
        monkeypatch,
        lambda _url: _FakeResponse(200, {"choices": [{"message": {"content": "   "}}]}),
    )

    with pytest.raises(AIUpstreamError) as exc_info:
        asyncio.run(GroqAIClient().create_chat_completion("sys", "hi"))

    assert exc_info.value.code == AIErrorCode.INVALID_RESPONSE
    assert exc_info.value.status_code == 502


def test_a_successful_response_returns_the_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch)
    _patch_transport(
        monkeypatch,
        lambda _url: _FakeResponse(200, {"choices": [{"message": {"content": "  resposta  "}}]}),
    )

    assert asyncio.run(GroqAIClient().create_chat_completion("sys", "hi")) == "resposta"


# ---------------------------------------------------------------------------
# 4. The JSON error contract seen by the app
# ---------------------------------------------------------------------------


class _StubRequest:
    """The AI handler only reads ``request.url.path``."""

    url = SimpleNamespace(path="/v1/objectives/1/roadmap")


async def _render(exc: AIUpstreamError):
    """Run the registered AI handler the way Starlette would."""
    app = FastAPI()
    register_exception_handlers(app)
    handler = app.exception_handlers[AIUpstreamError]
    return await handler(_StubRequest(), exc)


def test_the_handler_returns_detail_and_code() -> None:
    """The mobile client branches on ``code``; the shape must be exactly this."""
    error = AIUpstreamError(AIErrorCode.TIMEOUT)
    response = asyncio.run(_render(error))

    assert response.status_code == 504
    payload = json.loads(response.body)
    assert set(payload) == {"detail", "code"}
    assert payload["code"] == "ai_provider_timeout"
    assert payload["detail"] == error.detail


def test_the_handler_sets_the_status_from_the_code() -> None:
    for code in AIErrorCode:
        status = asyncio.run(_render(AIUpstreamError(code))).status_code
        assert status == AIUpstreamError(code).status_code


def test_the_handler_forwards_retry_after() -> None:
    """The app shows a countdown, so the header must survive the handler."""
    response = asyncio.run(_render(AIUpstreamError(AIErrorCode.BUSY, headers={"Retry-After": "5"})))
    assert response.headers.get("Retry-After") == "5"


def test_a_generic_http_error_gains_a_stable_code() -> None:
    """Every error body now carries ``code`` derived from the status.

    The AI/plan handlers already emitted a ``code``; this extends the same
    contract to plain ``HTTPException`` raises so the app never has to match
    on the (translated) human-readable ``detail``.
    """

    class _StubHTTPRequest:
        url = SimpleNamespace(path="/v1/whatever")

    app = FastAPI()
    register_exception_handlers(app)
    handler = app.exception_handlers[HTTPException]
    response = asyncio.run(handler(_StubHTTPRequest(), HTTPException(status_code=404, detail="Não encontrado")))

    assert json.loads(response.body) == {"detail": "Não encontrado", "code": "not_found"}


def test_a_busy_error_reaches_the_client_as_a_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end shape for the queue-saturated case."""
    _patch_settings(monkeypatch, groq_request_budget_seconds=6.0, groq_queue_wait_seconds=2.0)
    _patch_transport(
        monkeypatch,
        lambda _url: _FakeResponse(200, {"choices": [{"message": {"content": "hi"}}]}),
    )

    class _NeverFree:
        async def acquire(self, _tokens: int):
            await asyncio.Event().wait()

    monkeypatch.setattr(ai_client, "_get_groq_pacer", lambda: _NeverFree())

    async def scenario():
        with pytest.raises(AIUpstreamError) as exc_info:
            await GroqAIClient().create_chat_completion("sys", "hi")
        return await _render(exc_info.value)

    response = asyncio.run(scenario())
    assert response.status_code == 429
    assert json.loads(response.body)["code"] == "ai_busy"
    assert response.headers["Retry-After"] == "5"


def test_the_error_codes_are_distinct() -> None:
    """Two codes sharing a value would make the app's mapping ambiguous."""
    values = [code.value for code in AIErrorCode]
    assert len(values) == len(set(values))
