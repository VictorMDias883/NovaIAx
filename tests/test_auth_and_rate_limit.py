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

import asyncio

import pytest
from app.main import app
from fastapi.testclient import TestClient


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

    Sends 61 GET requests to a rate-limited endpoint (the default rate
    limit is 60 requests per minute).  The loop breaks as soon as a
    429 is received, so the final assertion checks that the last
    response was indeed 429.

    ``/health`` is deliberately *not* used here — it is exempted from
    rate limiting so orchestrator probes can never be throttled.
    """
    for _ in range(61):
        response = client.get("/rate-limit-probe")
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


def test_logout_revokes_refresh_token(client: TestClient) -> None:
    """A refresh token used against /logout can no longer refresh afterwards.

    Logout must revoke the token's ``jti`` in the denylist; a subsequent
    call to ``POST /api/v1/auth/refresh`` with the same token must fail
    with 401 instead of issuing a new pair.
    """
    client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Logout User",
            "email": "logout@example.com",
            "password": "Password123",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "logout@example.com", "password": "Password123"},
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 200

    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401


def test_refresh_token_still_valid_when_not_logged_out(client: TestClient) -> None:
    """A refresh token not revoked via /logout continues to work."""
    client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Refresh User",
            "email": "refresh@example.com",
            "password": "Password123",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "refresh@example.com", "password": "Password123"},
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 200


def test_api_key_authentication_returns_service_role() -> None:
    """Requests authenticated via the master API key get the SERVICE role.

    ``get_current_user`` must treat an ``X-API-Key`` identity as a
    service-level (non-user) role — the ``SERVICE`` member of
    ``UserRole`` — so role checks that enumerate enum members resolve
    correctly against a declared value.

    The API-key service is injected with a deterministic master key so the
    test is independent of ``MASTER_API_KEY`` in the local ``.env``.
    """
    from app.api.deps import get_current_user
    from app.core.config import Settings
    from app.core.security import ApiKeyService
    from starlette.datastructures import Headers
    from starlette.requests import Request

    master_key = "replace-with-strong-key"

    async def run() -> dict[str, object]:
        request = Request(
            scope={
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": Headers({"x-api-key": master_key}).raw,
                "scheme": "http",
                "server": ("testserver", 80),
                "query_string": b"",
                "client": ("127.0.0.1", 12345),
                "root_path": "",
            }
        )
        api_key_service = ApiKeyService(settings=Settings(master_api_key=master_key))
        return await get_current_user(request, api_key_service=api_key_service)

    identity = asyncio.run(run())
    assert identity["role"] == "SERVICE"
