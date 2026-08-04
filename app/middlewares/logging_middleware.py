"""
Request logging middleware.

This middleware measures the duration of each request and logs a
structured (JSON) record containing the HTTP method, path, response
status code, duration, and client IP.  The log is emitted *after*
the response is generated, so it captures the full request lifecycle.

The log output is consumed by the JSON formatter configured in
:mod:`app.core.logging`, making it easy to parse and aggregate in
log-management systems.
"""

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.logging import get_logger

logger = get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware that logs each completed request."""

    async def dispatch(self, request: Request, call_next):
        """Time the request and log the result.

        Args:
            request: The incoming :class:`Request`.
            call_next: A callable that forwards the request to the
                next middleware or route handler.

        Returns:
            The :class:`Response` from the downstream handler, with
            no modifications.
        """
        # Record the start time before forwarding the request.
        started = time.time()

        # Forward the request to the next middleware / route handler.
        response = await call_next(request)

        # Calculate the total duration in milliseconds (rounded to 2
        # decimal places for readability).
        duration_ms = round((time.time() - started) * 1000, 2)

        # Emit a structured log record with request metadata.
        logger.info(
            "request_completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": request.client.host if request.client else None,
            },
        )
        return response
