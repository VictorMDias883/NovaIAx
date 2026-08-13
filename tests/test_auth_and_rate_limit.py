"""
Integration tests for authentication and rate-limiting.

These tests use FastAPI's :class:`TestClient` (which runs the ASGI app
in-process) to verify:

1. **Login flow** — The default admin account can authenticate via the
   v1 ``POST /api/v1/auth/login`` endpoint and receive JWT tokens.

2. **Rate limiting** — After exceeding the default rate limit (60
   requests per minute), the gateway returns a 429 Too Many Requests
   response.

The tests import the FastAPI ``app`` instance directly from
:mod:`app.main`, so they exercise the full middleware stack
(including CORS, security headers, logging, auth, and rate limiting).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    """Provide a :class:`TestClient` instance for each test.

    The ``TestClient`` wraps the FastAPI app and allows synchronous
    HTTP requests to be made against it in-process (no network I/O).
    Using ``with TestClient(app)`` ensures the application lifespan
    events (startup/shutdown) are executed, creating database tables.
    """
    with TestClient(app) as c:
        yield c


def test_login_returns_tokens(client: TestClient) -> None:
    """Verify that a registered user can log in and receive JWT tokens.

    Sends a POST to ``/api/v1/auth/register`` followed by ``/api/v1/auth/login``.
    Asserts that the login response is 200 OK and contains both
    ``access_token`` and ``refresh_token``.
    """
    client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Admin Test",
            "email": "admin@example.com",
            "password": "Password123",
        },
    )
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "Password123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert "refresh_token" in body


def test_rate_limit_returns_429(client: TestClient) -> None:
    """Verify that the rate limiter returns 429 after exceeding the limit.

    Sends 61 GET requests to ``/health`` (the default rate limit is
    60 requests per minute).  The loop breaks as soon as a 429 is
    received.  The final assertion checks that the last response was
    indeed a 429.
    """
    for _ in range(61):
        response = client.get("/health")
        if response.status_code == 429:
            break
    assert response.status_code == 429


def test_allowed_origins_supports_csv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOWED_ORIGINS may be provided as a CSV string in the .env file.

    This matches the common Docker / environment-file format, where a list of
    origins is represented as ``http://localhost:3000,http://127.0.0.1:3000``
    instead of JSON.
    """
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

    from app.core.config import Settings

    settings = Settings()
    assert settings.allowed_origins == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
