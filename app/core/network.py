"""
Network helper utilities.

This module centralises client-IP resolution so that middleware (rate
limiter, logging) and handlers (proxy header injection) all agree on the
"real" client address for an incoming request.

On Fly.io every request arrives through Fly's edge proxy, so
``request.client.host`` is always the proxy's address, not the user's.
Fly sets the real client IP in the ``Fly-Client-IP`` header and also
appends to ``X-Forwarded-For``.  However, trusting those headers is only
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

    1. ``Fly-Client-IP`` — set automatically by Fly.io's edge proxy.
    2. The first IP in ``X-Forwarded-For`` — standard reverse-proxy
       convention (leftmost entry is the original client).
    3. ``request.client.host`` — direct connection, no proxy involved.

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
    # Fly.io edge proxy) is guaranteed to be in front of the app.
    if settings.trust_proxy_headers:
        fly_client_ip = request.headers.get("Fly-Client-IP")
        if fly_client_ip and fly_client_ip.strip():
            return fly_client_ip.strip()

        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for and forwarded_for.strip():
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                return first_ip

    return request.client.host if request.client else "unknown"
