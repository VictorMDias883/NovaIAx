"""
Typed, machine-readable failures for every AI-backed route.

The mobile app has to react differently to each way an AI call can fail
(retry now, retry later, show a maintenance message, ask the user to try a
rephrasing), so a bare HTTP status is not enough — it needs a stable ``code``
to switch on.  This module gives those failures a single vocabulary:

    =========================  ======  ==================================
    ``code``                   status  when it happens
    =========================  ======  ==================================
    ``ai_rate_limited``        429     A provider's own quota is exhausted.
    ``ai_busy``               429     Our pacer/concurrency queue is full
                                      and the request waited as long as it
                                      is allowed to.
    ``ai_quota_exceeded``      429     The user's per-plan daily AI quota is
                                      exhausted (``Retry-After`` = seconds
                                      until the UTC reset).
    ``ai_provider_timeout``    504     The provider did not answer in time.
    ``ai_provider_error``      502     The provider failed (5xx) or the
                                      connection broke.
    ``ai_provider_auth``       502     Our API key was rejected by a provider.
    ``ai_invalid_response``    502     A provider answered with something
                                      we cannot use as assistant text.
    ``ai_all_providers_failed`` 502    Every configured provider failed (or
                                      none is configured at all).
    =========================  ======  ==================================

Every message is written in pt-BR because it is surfaced verbatim to the
end user by the app when no localised copy is available.

:class:`AIUpstreamError` subclasses :class:`fastapi.HTTPException`, so it can
be raised from deep inside the client/service layers without those layers
importing anything FastAPI-response-shaped, and
:func:`app.exceptions.handlers.register_exception_handlers` renders it as
``{"detail": ..., "code": ...}`` — an extra ``code`` key the generic
``HTTPException`` handler does not emit.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class AIErrorCode(str, Enum):
    """Stable identifiers the mobile app maps to user-facing behaviour."""

    #: Groq answered 429: the organization-wide quota (RPM/RPD/TPM/TPD) is
    #: exhausted.  Retrying soon cannot help — the window is up to a day.
    RATE_LIMITED = "ai_rate_limited"
    #: The in-process pacer/concurrency queue did not free a slot within the
    #: configured budget.  Other users are using the AI right now; a short
    #: wait is enough.
    BUSY = "ai_busy"
    #: The user's daily per-plan AI quota (messages and/or tokens) is spent
    #: for today.  ``Retry-After`` carries the seconds until the UTC reset.
    QUOTA_EXCEEDED = "ai_quota_exceeded"
    #: The provider did not answer within ``groq_api_timeout_seconds``.
    TIMEOUT = "ai_provider_timeout"
    #: The provider is down or returned an error response.
    PROVIDER_ERROR = "ai_provider_error"
    #: Our ``GROQ_API_KEY`` was rejected — a server-side misconfiguration,
    #: never something the end user can fix.
    PROVIDER_AUTH = "ai_provider_auth_failed"
    #: The provider replied with a body we cannot turn into assistant text
    #: (invalid JSON, no choices, empty content).
    INVALID_RESPONSE = "ai_invalid_response"
    #: Every configured provider failed (or the deployment has none).  The
    #: request was never answered, so nothing user-side is broken.
    ALL_PROVIDERS_FAILED = "ai_all_providers_failed"


#: Default pt-BR message per code, used when a call site does not supply a
#: more specific one (e.g. Groq's own error text appended to a 429).
DEFAULT_AI_MESSAGES: dict[AIErrorCode, str] = {
    AIErrorCode.RATE_LIMITED: (
        "O serviço de inteligência artificial está com a cota esgotada no momento. "
        "Tente novamente mais tarde."
    ),
    AIErrorCode.BUSY: (
        "Muitos usuários usando a inteligência artificial ao mesmo tempo. "
        "Tente novamente em alguns segundos."
    ),
    AIErrorCode.QUOTA_EXCEEDED: (
        "Você atingiu o limite de uso da inteligência artificial no seu plano "
        "por hoje. O limite é renovado automaticamente à meia-noite."
    ),
    AIErrorCode.TIMEOUT: "O serviço de inteligência artificial demorou demais para responder. Tente novamente.",
    AIErrorCode.PROVIDER_ERROR: "O serviço de inteligência artificial está indisponível. Tente novamente mais tarde.",
    AIErrorCode.PROVIDER_AUTH: "O serviço de inteligência artificial está temporariamente indisponível.",
    AIErrorCode.INVALID_RESPONSE: "O serviço de inteligência artificial retornou uma resposta inesperada. Tente novamente.",
    AIErrorCode.ALL_PROVIDERS_FAILED: (
        "O serviço de inteligência artificial está indisponível no momento. "
        "Tente novamente mais tarde."
    ),
}


class AIUpstreamError(HTTPException):
    """An AI provider failure carrying a stable, app-mappable ``code``.

    Args:
        code: The :class:`AIErrorCode` identifying the failure class.
        detail: pt-BR message shown to the user.  Defaults to the message
            registered for ``code``.
        headers: Optional response headers (e.g. ``Retry-After``).
        provider_message: The provider's own error text, if any.  It is
            logged so support can see what Groq actually complained about, and
            appended to the default ``detail`` so the pt-BR sentence is kept —
            see below.
    """

    def __init__(
        self,
        code: AIErrorCode,
        *,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
        provider_message: str | None = None,
    ) -> None:
        self.code = code
        self.provider_message = provider_message
        status_code = _STATUS_BY_CODE[code]
        if detail is None:
            detail = DEFAULT_AI_MESSAGES[code]
            if provider_message:
                # Groq explains exactly which limit it hit ("on tokens per
                # minute (TPM): Limit 8000, Requested ~12903"), which is
                # invaluable when tuning the pacer.  It is appended to the
                # user-facing sentence instead of replacing it, so the app
                # still shows a pt-BR message and still switches on ``code``.
                detail = f"{detail} ({provider_message})"
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers=headers,
        )


_STATUS_BY_CODE: dict[AIErrorCode, int] = {
    AIErrorCode.RATE_LIMITED: 429,
    AIErrorCode.BUSY: 429,
    AIErrorCode.QUOTA_EXCEEDED: 429,
    AIErrorCode.TIMEOUT: 504,
    AIErrorCode.PROVIDER_ERROR: 502,
    AIErrorCode.PROVIDER_AUTH: 502,
    AIErrorCode.INVALID_RESPONSE: 502,
    AIErrorCode.ALL_PROVIDERS_FAILED: 502,
}
