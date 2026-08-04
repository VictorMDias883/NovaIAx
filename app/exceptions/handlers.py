"""
Global exception handlers.

This module registers custom exception handlers on the FastAPI
application.  These handlers intercept exceptions that would otherwise
produce default error responses and replace them with consistent,
structured JSON responses.  They also log relevant information for
debugging.

Three handlers are registered:

1. **RequestValidationError** (422)
    Triggered when the request body or query parameters fail Pydantic
    validation.  The handler logs the validation errors and returns a
    generic ``{"detail": "Invalid request"}`` message (without exposing
    the specific validation errors to the client, for security).

2. **HTTPException** (variable status code)
    Triggered by explicit ``raise HTTPException(...)`` calls in route
    handlers.  The handler logs the status code and returns the
    exception's ``detail`` message.

3. **Unhandled Exception** (500)
    A catch-all for any exception not handled by the above.  The
    handler logs the full traceback and returns a generic
    ``{"detail": "Internal server error"}`` message (without exposing
    internal details to the client).
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

from app.core.logging import get_logger

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
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": "Invalid request"})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle explicit ``HTTPException`` raises (e.g. 401, 404, 409).

        Logs the status code and returns the exception's detail message.
        """
        logger.warning("http_exception", extra={"path": request.url.path, "status_code": exc.status_code})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all handler for unhandled exceptions (500 Internal Server Error).

        Logs the full traceback (via ``logger.exception``) and returns
        a generic error message.  Internal details are never exposed
        to the client.
        """
        logger.exception("Unhandled exception", extra={"path": request.url.path})
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal server error"})
