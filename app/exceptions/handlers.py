"""
Global exception handlers.

This module registers custom exception handlers on the FastAPI
application.  These handlers intercept exceptions that would otherwise
produce default error responses and replace them with consistent,
structured JSON responses.  They also log relevant information for
debugging.

Four handlers are registered:

1. **RequestValidationError** (422)
    Triggered when the request body or query parameters fail Pydantic
    validation.  The handler logs the validation errors and returns a
    generic ``{"detail": "Invalid request"}`` message (without exposing
    the specific validation errors to the client, for security).

2. **HTTPException** (variable status code)
    Triggered by explicit ``raise HTTPException(...)`` calls in route
    handlers.  The handler logs the status code and returns the
    exception's ``detail`` message plus a stable ``code`` derived from the
    status (or from the exception's own ``code`` when it is an
    :class:`~app.exceptions.api_errors.APIError`).

3. **Unhandled Exception** (500)
    A catch-all for any exception not handled by the above.  The
    handler logs the full traceback and returns a generic
    ``{"detail": "Erro interno do servidor", "code": "internal_error"}``
    message (without exposing internal details to the client).

4. **AIUpstreamError** (429 / 502 / 504)
    Raised by :mod:`app.clients.ai_client` when the AI provider fails.  It
    is rendered as ``{"detail": <pt-BR message>, "code": <stable code>}`` so
    the mobile app can map the failure to the right behaviour instead of
    pattern-matching a human-readable message.

5. **PlanRequiredError** (402 / 403 / 400 / 503)
    Raised by :func:`app.api.deps.require_plan` and the billing service when
    a feature is locked, a plan ceiling is hit or a purchase cannot be
    verified.  Rendered as ``{"detail", "code", "feature", "current_plan",
    "required_plan"}`` so the app can show the right paywall.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

from app.core.logging import get_logger
from app.exceptions.ai_errors import AIUpstreamError
from app.exceptions.api_errors import APIErrorCode, code_for_status
from app.exceptions.plan_errors import PlanRequiredError

logger = get_logger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all custom exception handlers on the FastAPI app.

    This function is called from :mod:`app.main` after the app is
    created.  Each handler is registered as a closure inside this
    function so that it has access to the ``app`` instance.

    Args:
        app: The :class:`FastAPI` application instance.
    """

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Handle Pydantic validation errors (422 Unprocessable Entity).

        Logs the validation errors (including the request path) and
        returns a generic error message to the client.
        """
        logger.warning("validation_error", extra={"path": request.url.path, "errors": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": "Requisição inválida.",
                "code": APIErrorCode.VALIDATION_ERROR.value,
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle explicit ``HTTPException`` raises (e.g. 401, 404, 409).

        Logs the status code and returns the exception's detail message.
        """
        logger.warning("http_exception", extra={"path": request.url.path, "status_code": exc.status_code})
        # ``AIUpstreamError``/``PlanRequiredError`` have their own handlers
        # below; this covers every plain ``HTTPException`` (and any ``APIError``
        # whose ``code`` should win over the status default).
        code = getattr(exc, "code", None) or code_for_status(exc.status_code)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "code": code})

    @app.exception_handler(AIUpstreamError)
    async def ai_upstream_exception_handler(request: Request, exc: AIUpstreamError) -> JSONResponse:
        """Render an AI provider failure with its machine-readable ``code``.

        Dispatching happens on the most specific registered class in the
        exception's MRO, so this handler wins for :class:`AIUpstreamError`
        while every other ``HTTPException`` keeps the plain
        ``{"detail": ...}`` body produced by the handler above.  The
        provider's own message is logged for support, never returned.
        """
        logger.warning(
            "ai_upstream_error",
            extra={
                "path": request.url.path,
                "status_code": exc.status_code,
                "code": exc.code.value,
                "provider_message": exc.provider_message,
            },
        )
        content: dict[str, str] = {"detail": str(exc.detail), "code": exc.code.value}
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(PlanRequiredError)
    async def plan_required_exception_handler(request: Request, exc: PlanRequiredError) -> JSONResponse:
        """Render a plan-gating/billing failure with its machine-readable ``code``.

        The response carries just enough for the app to pick a paywall:
        which feature was refused, the user's current plan and the cheapest
        plan that would unlock it.
        """
        logger.warning(
            "plan_required",
            extra={
                "path": request.url.path,
                "status_code": exc.status_code,
                "code": exc.code.value,
                "feature": exc.feature,
                "current_plan": exc.current_plan,
                "required_plan": exc.required_plan,
            },
        )
        content: dict[str, object] = {"detail": str(exc.detail), "code": exc.code.value}
        if exc.feature is not None:
            content["feature"] = exc.feature
        if exc.current_plan is not None:
            content["current_plan"] = exc.current_plan
        if exc.required_plan is not None:
            content["required_plan"] = exc.required_plan
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all handler for unhandled exceptions (500 Internal Server Error).

        Logs the full traceback (via ``logger.exception``) and returns
        a generic error message.  Internal details are never exposed
        to the client.
        """
        logger.exception("Unhandled exception", extra={"path": request.url.path})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "Erro interno do servidor.",
                "code": APIErrorCode.INTERNAL_ERROR.value,
            },
        )
