"""
Network helper utilities.

This module centralises client-IP resolution so that middleware (rate
limiter, logging) and handlers (proxy header injection) all agree on the
"real" client address for an incoming request.

Behind Render's edge proxy every request arrives with
``request.client.host`` set to the proxy's address, not the user's.
Render's load balancer terminates the client connection and sets (not
appends to) the ``X-Forwarded-For`` header to the client's real IP for
every request — there is exactly one trusted hop, so the first/only entry
in the header is safe to trust.  However, trusting proxy headers is only
safe when a trusted reverse proxy is guaranteed to sit in front of the
app; otherwise a malicious client could spoof its apparent IP by
injecting arbitrary header values.  The :attr:`Settings.trust_proxy_headers`
flag controls whether the headers are trusted.
"""

from fastapi import Request

from app.core.config import Settings, get_settings


def get_client_ip(request: Request, settings: Settings | None = None) -> str:
    """Resolve the real client IP for an incoming request.

    When ``settings.trust_proxy_headers`` is enabled, the following
    precedence is used:

    1. The first IP in ``X-Forwarded-For`` — set by Render's edge proxy
       (single trusted hop; the leftmost entry is the original client).
    2. ``request.client.host`` — direct connection, no proxy involved.

    When ``settings.trust_proxy_headers`` is disabled (the default), the
    proxy headers are **ignored entirely** and ``request.client.host`` is
    used, so a malicious client cannot spoof its IP in environments
    without a trusted reverse proxy in front.

    Args:
        request: The incoming :class:`Request`.
        settings: Optional :class:`Settings`; defaults to the cached
            singleton.

    Returns:
        The resolved client IP as a string, or ``"unknown"`` if no client
        address is available.
    """
    settings = settings or get_settings()

    # Only trust proxy-set headers when a trusted reverse proxy (e.g. the
    # Render edge proxy) is guaranteed to be in front of the app.  Render
    # overwrites ``X-Forwarded-For`` for every request it forwards, so the
    # first entry is the real client and cannot be spoofed from outside.
    if settings.trust_proxy_headers:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for and forwarded_for.strip():
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                return first_ip

    return request.client.host if request.client else "unknown"
