"""
Security-headers middleware.

This middleware injects a set of HTTP security headers into every
response.  These headers help protect the application against common
web vulnerabilities:

    - ``X-Content-Type-Options: nosniff``
        Prevents browsers from MIME-sniffing the response content type.
        This stops attacks where a non-script file is interpreted as
        JavaScript.

    - ``X-Frame-Options: DENY``
        Prevents the page from being embedded in an ``<iframe>``,
        mitigating clickjacking attacks.

    - ``Strict-Transport-Security: max-age=31536000; includeSubDomains``
        Tells browsers to only connect via HTTPS for the next year
        (31,536,000 seconds).  ``includeSubDomains`` extends this to
        all subdomains.

    - ``Referrer-Policy: no-referrer``
        Prevents the browser from sending the ``Referer`` header to
        other origins, protecting user privacy.

    - ``Permissions-Policy: geolocation=(), microphone=()``
        Disables access to the Geolocation and Microphone APIs,
        reducing the attack surface.

This middleware is applied to all responses, including error responses.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that adds security-related HTTP headers to responses."""

    async def dispatch(self, request: Request, call_next):
        """Forward the request and inject security headers into the response.

        Args:
            request: The incoming :class:`Request`.
            call_next: A callable that forwards the request to the
                next middleware or route handler.

        Returns:
            The :class:`Response` with security headers added.
        """
        # Forward the request to the downstream handler.
        response: Response = await call_next(request)

        # Inject security headers into the response.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
        return response
