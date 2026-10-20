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
from datetime import UTC, datetime

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
            "birth_date": "1990-01-01",
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
            "birth_date": "1990-01-01",
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
            "birth_date": "1990-01-01",
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


def test_a_refresh_token_minted_in_the_same_second_as_the_user_is_accepted(client: TestClient) -> None:
    """Second-precision mismatch between ``iat`` and ``refresh_token_valid_after``.

    A JWT ``iat`` is an integer by specification, while
    ``refresh_token_valid_after`` is a timestamp with microsecond precision on
    PostgreSQL.  Comparing them directly rejected any token issued in the same
    second the account was created or its password was changed, so a user had
    to wait a second before their first refresh worked.  SQLite's
    ``CURRENT_TIMESTAMP`` has no fractional part, which is why the suite never
    saw it.
    """
    from app.core.security import decode_token
    from app.db.session import SessionLocal
    from app.models.user import User
    from sqlalchemy import update

    client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Same Second",
            "email": "same-second@example.com",
            "password": "Password123",
            "birth_date": "1990-01-01",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "same-second@example.com", "password": "Password123"},
    )
    assert login.status_code == 200
    refresh_token = login.json()["refresh_token"]

    # A microsecond-precision timestamp inside the token's own second.
    issued_second = datetime.fromtimestamp(decode_token(refresh_token)["iat"], tz=UTC)
    stamped = issued_second.replace(microsecond=750_000)

    async def _stamp() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(User)
                .where(User.email == "same-second@example.com")
                .values(refresh_token_valid_after=stamped)
            )
            await session.commit()

    asyncio.run(_stamp())

    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 200, "a token issued in the same second must be accepted"
    assert refresh.json()["access_token"]


def test_a_refresh_token_older_than_the_password_change_is_still_rejected(client: TestClient) -> None:
    """The fix must not weaken the invalidation it was protecting.

    A token issued a whole second *before* ``refresh_token_valid_after`` is
    the case the timestamp exists for: the password changed after the token was
    minted, so it must stop working.
    """
    from app.core.security import decode_token
    from app.db.session import SessionLocal
    from app.models.user import User
    from sqlalchemy import update

    client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Older Token",
            "email": "older-token@example.com",
            "password": "Password123",
            "birth_date": "1990-01-01",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "older-token@example.com", "password": "Password123"},
    )
    refresh_token = login.json()["refresh_token"]
    issued_at = decode_token(refresh_token)["iat"]
    changed = datetime.fromtimestamp(issued_at + 1, tz=UTC)

    async def _stamp() -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(User)
                .where(User.email == "older-token@example.com")
                .values(refresh_token_valid_after=changed)
            )
            await session.commit()

    asyncio.run(_stamp())

    refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401


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


def test_the_ai_bucket_is_not_polluted_by_ordinary_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ten normal requests must not lock a user out of the assistant.

    Both buckets used a single sorted set keyed only by IP, so ordinary traffic
    filled the counter and the stricter AI limit was then applied to *that*
    total: a user who made ten normal requests in a minute got a 429 from every
    AI endpoint.  Each bucket now has its own window.
    """
    from app.cache.redis_client import RedisClient
    from app.core.config import Settings
    from app.middlewares.rate_limit_middleware import RateLimitMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    settings = Settings(rate_limit_default=60, rate_limit_ai=3, redis_url="redis://127.0.0.1:1/0")
    RedisClient.reset_all_memory_stores()

    async def plain(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    async def ai(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/plain", plain),
            Route("/api/v1/agents/general/chat", ai),
        ]
    )
    app.add_middleware(RateLimitMiddleware)

    # The middleware reads the process-wide settings singleton.
    monkeypatch.setattr("app.middlewares.rate_limit_middleware.get_settings", lambda: settings)

    with TestClient(app) as probe:
        general = [probe.get("/plain").status_code for _ in range(20)]
        assert set(general) == {200}, f"ordinary traffic must stay under the general limit: {general}"

        ai_statuses = [probe.get("/api/v1/agents/general/chat").status_code for _ in range(5)]

    assert ai_statuses[0] == 200, "AI must not be blocked by ordinary traffic"
    assert 429 in ai_statuses, "the AI bucket must still enforce its own stricter limit"
    assert ai_statuses.count(200) == 3, f"exactly three AI calls fit the limit of 3: {ai_statuses}"


def test_ordinary_traffic_is_not_polluted_by_ai_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse direction: AI calls must not consume the general allowance."""
    from app.cache.redis_client import RedisClient
    from app.core.config import Settings
    from app.middlewares.rate_limit_middleware import RateLimitMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    settings = Settings(rate_limit_default=20, rate_limit_ai=2, redis_url="redis://127.0.0.1:1/0")
    RedisClient.reset_all_memory_stores()

    async def plain(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    async def ai(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/plain", plain), Route("/api/v1/agents/general/chat", ai)])
    app.add_middleware(RateLimitMiddleware)
    monkeypatch.setattr("app.middlewares.rate_limit_middleware.get_settings", lambda: settings)

    with TestClient(app) as probe:
        for _ in range(5):
            assert probe.get("/api/v1/agents/general/chat").status_code in (200, 429)
        general = [probe.get("/plain").status_code for _ in range(20)]

    assert set(general) == {200}, f"AI calls must not eat the general allowance: {general}"


def test_the_ai_bucket_is_keyed_per_user_not_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticated AI traffic is metered per user id, not per client IP.

    Users behind one NAT/office IP must each get their own budget, and a single
    account cannot multiply its provider-quota share by rotating IPs.  Here both
    users share the TestClient's client IP, yet each has an independent window.
    """
    from app.cache.redis_client import RedisClient
    from app.core.config import Settings
    from app.core.security import create_access_token
    from app.middlewares.rate_limit_middleware import RateLimitMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    settings = Settings(
        rate_limit_default=60,
        rate_limit_ai=10,
        rate_limit_ai_user=2,
        redis_url="redis://127.0.0.1:1/0",
    )
    RedisClient.reset_all_memory_stores()

    async def ai(request):  # noqa: ANN001
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/agents/general/chat", ai)])
    app.add_middleware(RateLimitMiddleware)
    monkeypatch.setattr("app.middlewares.rate_limit_middleware.get_settings", lambda: settings)

    headers_a = {"Authorization": f"Bearer {create_access_token('1', email='a@x.com', role='USER')}"}
    headers_b = {"Authorization": f"Bearer {create_access_token('2', email='b@x.com', role='USER')}"}

    with TestClient(app) as probe:
        statuses_a = [probe.get("/api/v1/agents/general/chat", headers=headers_a).status_code for _ in range(3)]
        statuses_b = [probe.get("/api/v1/agents/general/chat", headers=headers_b).status_code for _ in range(3)]

    assert statuses_a == [200, 200, 429], statuses_a
    assert statuses_b == [200, 200, 429], statuses_b
