"""
Stable, machine-readable ``code`` for the generic (non-AI, non-plan) errors.

The AI (:mod:`app.exceptions.ai_errors`) and billing
(:mod:`app.exceptions.plan_errors`) layers already answer with a ``code`` the
app switches on.  Every other failure — auth, ownership, validation, conflict —
used to arrive as a bare ``{"detail": "..."}``, so the client could only match
on the human-readable message, which is translated and therefore unstable.

This module closes that gap.  The error envelope every handler emits is::

    {"detail": "<pt-BR message>", "code": "<stable code>"}

``detail`` stays exactly as before (additive change — a client that only reads
``detail`` keeps working); ``code`` is the new contract documented in
``FLUTTER_HANDOFF.md``.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException

__all__ = ["APIError", "APIErrorCode", "code_for_status"]


class APIErrorCode(str, Enum):
    """Stable identifiers for the generic error classes."""

    AUTHENTICATION_REQUIRED = "authentication_required"
    INVALID_CREDENTIALS = "invalid_credentials"
    FORBIDDEN = "forbidden"
    ACCOUNT_SUSPENDED = "account_suspended"
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    BAD_REQUEST = "bad_request"
    CONFLICT = "conflict"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


#: Default code for a status when the call site did not name one.  Keeps the
#: contract total: *every* error response carries a ``code``.
_STATUS_TO_CODE: dict[int, APIErrorCode] = {
    400: APIErrorCode.BAD_REQUEST,
    401: APIErrorCode.AUTHENTICATION_REQUIRED,
    403: APIErrorCode.FORBIDDEN,
    404: APIErrorCode.NOT_FOUND,
    409: APIErrorCode.CONFLICT,
    413: APIErrorCode.PAYLOAD_TOO_LARGE,
    422: APIErrorCode.VALIDATION_ERROR,
    429: APIErrorCode.RATE_LIMIT_EXCEEDED,
    500: APIErrorCode.INTERNAL_ERROR,
    502: APIErrorCode.SERVICE_UNAVAILABLE,
    503: APIErrorCode.SERVICE_UNAVAILABLE,
}


def code_for_status(status_code: int) -> str:
    """Return the default stable code for an HTTP status.

    Args:
        status_code: The HTTP status code of the response.

    Returns:
        The matching :class:`APIErrorCode` value, or ``"error"`` for a status
        outside the known set (so the field is never missing).
    """
    return _STATUS_TO_CODE.get(status_code, APIErrorCode.BAD_REQUEST).value


class APIError(HTTPException):
    """An ``HTTPException`` that carries an explicit ``code``.

    Use this instead of a bare :class:`~fastapi.HTTPException` when the status
    alone does not identify the failure (for example a 401 meaning "bad
    credentials" versus one meaning "missing token").

    Args:
        status_code: The HTTP status code.
        detail: pt-BR message shown to the user.
        code: The :class:`APIErrorCode` to expose (defaults to the status's
            generic code).
        headers: Optional response headers (e.g. ``WWW-Authenticate``).
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: APIErrorCode | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = (code or _STATUS_TO_CODE.get(status_code, APIErrorCode.BAD_REQUEST)).value
        super().__init__(status_code=status_code, detail=detail, headers=headers)
