"""
Unit tests for :func:`app.core.network.get_client_ip`.

These tests build :class:`Request` objects directly (no ASGI server) and
verify the client-IP resolution precedence:

* ``X-Forwarded-For`` (first entry) beats ``request.client.host`` when
  ``trust_proxy_headers`` is enabled.
* Proxy headers are **ignored** when ``trust_proxy_headers`` is disabled,
  so a malicious direct client cannot spoof its IP.
"""

from app.core.config import Settings
from app.core.network import get_client_ip
from fastapi import Request


def make_request(
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("direct-host", 12345),
) -> Request:
    """Build a :class:`Request` from a raw ASGI scope (no server needed)."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": client,
        "server": ("testserver", 80),
    }
    return Request(scope)


def trusted_settings() -> Settings:
    """Settings with proxy-header trust enabled (Render deployment)."""
    return Settings(trust_proxy_headers=True)


def untrusted_settings() -> Settings:
    """Settings with proxy-header trust disabled (local dev default)."""
    return Settings(trust_proxy_headers=False)


# --- Precedence when a trusted reverse proxy is configured ---------------


def test_uses_first_forwarded_for_ip() -> None:
    request = make_request(
        headers={"X-Forwarded-For": "198.51.100.2, 192.0.2.1"}
    )
    assert get_client_ip(request, trusted_settings()) == "198.51.100.2"


def test_uses_forwarded_for_ip_when_no_proxy_header_whitespace() -> None:
    request = make_request(
        headers={"X-Forwarded-For": "  198.51.100.2 , 192.0.2.1  "}
    )
    assert get_client_ip(request, trusted_settings()) == "198.51.100.2"


def test_falls_back_to_peer_host_when_no_headers() -> None:
    request = make_request(client=("192.0.2.77", 54321))
    assert get_client_ip(request, trusted_settings()) == "192.0.2.77"


def test_returns_unknown_when_no_client_and_no_headers() -> None:
    request = make_request(client=None)
    assert get_client_ip(request, trusted_settings()) == "unknown"


# --- Proxy headers must NOT be trusted without a trusted proxy -----------


def test_ignores_proxy_headers_when_not_trusted() -> None:
    request = make_request(
        headers={"X-Forwarded-For": "198.51.100.2"},
        client=("spoofed-host", 12345),
    )
    assert get_client_ip(request, untrusted_settings()) == "spoofed-host"


def test_returns_peer_host_when_not_trusted_no_headers() -> None:
    request = make_request(client=("127.0.0.1", 54321))
    assert get_client_ip(request, untrusted_settings()) == "127.0.0.1"
