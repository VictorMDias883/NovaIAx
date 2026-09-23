"""
Regression tests for specific previously-fixed gateway bugs.

Each test in this module pins down a bug that was fixed and guards against
its reappearance:

1. API-key identities are rejected (403) on user-scoped endpoints instead
   of crashing with a ``ValueError`` when ``"api-key"`` is coerced to int.
2. ``GET /admin/login`` no longer 303-redirects a non-admin (or deleted
   user) holding a valid access-token cookie — that caused an infinite
   login loop between ``/admin/`` and ``/admin/login``.
3. The reverse-proxy cache is scoped per caller and replays raw bytes
   (base64) instead of ``errors="ignore"``-corrupted UTF-8 text — so
   cached responses can never leak between users or corrupt binary bodies.
"""

import asyncio
import uuid
from collections.abc import Generator

import pytest
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

_PASSWORD = "Password123"
_COOKIE = "novaiax_admin_session"


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a :class:`TestClient` running the whole app (lifespan included)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup() -> Generator[None, None, None]:
    """Delete the users created by this module so ``test.db`` stays deterministic."""

    def _purge() -> None:
        async def run() -> None:
            async with SessionLocal() as session:
                await session.execute(delete(User).where(User.email.like("%-fix-@example.com")))
                await session.commit()

        asyncio.run(run())

    yield
    _purge()


def _unique_email(prefix: str) -> str:
    return f"{prefix}-fix-{uuid.uuid4().hex[:8]}@example.com"


def _register(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Fix User", "email": email, "password": _PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _set_role(email: str, role: UserRole) -> None:
    async def run() -> None:
        async with SessionLocal() as session:
            await session.execute(update(User).where(User.email == email).values(role=role))
            await session.commit()

    asyncio.run(run())


def _user_exists(email: str) -> bool:
    async def run() -> bool:
        async with SessionLocal() as session:
            result = await session.execute(select(User.id).where(User.email == email))
            return result.scalars().first() is not None

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# 1. API-key identities must get a clean 403 on user-scoped endpoints
# ---------------------------------------------------------------------------


def test_api_key_gets_403_on_user_scoped_routes(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """API-key (SERVICE) identities must not crash int(user.id) coercion."""
    monkeypatch.setattr(get_settings(), "master_api_key", "test-master-key")
    headers = {"X-API-Key": "test-master-key"}

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 403, me.text

    listed = client.get("/api/v1/objectives/", headers=headers)
    assert listed.status_code == 403, listed.text

    registered = client.post(
        "/api/v1/objectives/register",
        headers=headers,
        json={"title": "Meta", "description": None, "due_date": "2027-12-31T00:00:00Z"},
    )
    assert registered.status_code == 403, registered.text


def test_jwt_users_still_work_on_user_scoped_routes(client: TestClient) -> None:
    """The 403 rule must apply only to API-key identities, not real users."""
    user = _register(client, _unique_email("jwt"))
    token = user["access_token"]

    me = client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == user["user"]["email"]

    listed = client.get("/api/v1/objectives/", headers=_auth(token))
    assert listed.status_code == 200, listed.text


# ---------------------------------------------------------------------------
# 2. /admin/login must not loop for a non-admin (or deleted) session cookie
# ---------------------------------------------------------------------------


def test_non_admin_cookie_renders_login_not_redirect(client: TestClient) -> None:
    """A valid access cookie for a non-admin must not bounce to /admin/."""
    non_admin = _register(client, _unique_email("user1"))
    user_email = non_admin["user"]["email"]
    _set_role(user_email, UserRole.USER)

    login = client.post("/api/v1/auth/login", json={"email": user_email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.cookies.set(_COOKIE, login.json()["access_token"])

    page = client.get("/admin/login", follow_redirects=False)
    assert page.status_code == 200, page.text
    assert 'action="/admin/login"' in page.text

    # The dashboard still refuses the non-admin cookie (→ login), so the
    # login page returning 200 breaks the redirect loop.
    dash = client.get("/admin/", follow_redirects=False)
    assert dash.status_code == 303
    assert dash.headers["location"] == "/admin/login"


def test_deleted_user_cookie_renders_login_not_redirect(client: TestClient) -> None:
    """A cookie for a user deleted from the database must not loop either."""
    doomed = _register(client, _unique_email("deluser"))
    login = client.post(
        "/api/v1/auth/login",
        json={"email": doomed["user"]["email"], "password": _PASSWORD},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    async def remove() -> None:
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email == doomed["user"]["email"]))
            await session.commit()

    asyncio.run(remove())
    assert not _user_exists(doomed["user"]["email"])

    client.cookies.set(_COOKIE, token)
    page = client.get("/admin/login", follow_redirects=False)
    assert page.status_code == 200, page.text
    assert 'action="/admin/login"' in page.text


def test_admin_cookie_shortcuts_to_dashboard(client: TestClient) -> None:
    """A real ADMIN's access cookie still redirects /admin/login → /admin/."""
    admin = _register(client, _unique_email("admin2"))
    _set_role(admin["user"]["email"], UserRole.ADMIN)

    client.cookies.set(_COOKIE, admin["access_token"])
    page = client.get("/admin/login", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/admin/"


# ---------------------------------------------------------------------------
# 3. Proxy cache: per-caller scoping + raw binary body replay
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": "application/octet-stream"}


class _SequenceClient:
    """Ai client that returns a distinct payload per downstream call."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls = 0

    async def __aenter__(self) -> "_SequenceClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(f"downstream-{self.calls}".encode())


class _BinaryClient:
    """Ai client that always returns the same binary payload."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls = 0

    async def __aenter__(self) -> "_BinaryClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(self._payload)


def _force_memory_redis(monkeypatch: pytest.MonkeyPatch) -> object:
    """Point the proxy's cache dependency at a shared in-memory Redis store.

    ``get_redis_client`` returns a fresh ``RedisClient()`` per request, so a
    per-request in-memory store would never persist across requests.  Since
    FastAPI captured that dependency function (and it resolves the ``RedisClient``
    class from its own module globals at call time), patching the class in
    ``app.api.deps`` makes every injected client resolve to one shared instance.
    """
    from app.cache.redis_client import RedisClient

    shared = RedisClient()
    shared._client = shared._memory_store  # force the in-memory backend
    monkeypatch.setattr("app.api.deps.RedisClient", lambda *a, **k: shared)
    return shared


def test_proxy_cache_is_scoped_per_user(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Cached proxy responses must never leak between different users."""
    downstream = _SequenceClient()
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: downstream)
    _force_memory_redis(monkeypatch)

    user_a = _register(client, _unique_email("proxy-a"))
    user_b = _register(client, _unique_email("proxy-b"))
    token_a, token_b = _auth(user_a["access_token"]), _auth(user_b["access_token"])

    first = client.get("/api/v1/proxy/ai/chat", headers=token_a)
    assert first.status_code == 200
    assert first.content == b"downstream-1"

    user_b_first = client.get("/api/v1/proxy/ai/chat", headers=token_b)
    assert user_b_first.content == b"downstream-2", "different user must be a cache miss"

    user_a_cached = client.get("/api/v1/proxy/ai/chat", headers=token_a)
    assert user_a_cached.headers.get("x-cache") == "HIT"
    assert user_a_cached.content == b"downstream-1", "user A must get its own cached body"

    user_b_cached = client.get("/api/v1/proxy/ai/chat", headers=token_b)
    assert user_b_cached.headers.get("x-cache") == "HIT"
    assert user_b_cached.content == b"downstream-2", "user B must get its own cached body"

    # Only the first call per user should have reached the downstream service.
    assert downstream.calls == 2


def test_proxy_cache_preserves_binary_body(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
) -> None:
    """Cached binary bodies must round-trip byte-for-byte (base64, not UTF-8)."""
    payload = b"\x00\xff\xfe\x10\x00AI\xf0\x9f\x92\xa9"
    downstream = _BinaryClient(payload)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: downstream)
    _force_memory_redis(monkeypatch)

    user = _register(client, _unique_email("proxy-bin"))
    token = _auth(user["access_token"])

    fresh = client.get("/api/v1/proxy/ai/blob", headers=token)
    assert fresh.status_code == 200
    assert fresh.content == payload

    cached = client.get("/api/v1/proxy/ai/blob", headers=token)
    assert cached.headers.get("x-cache") == "HIT"
    assert cached.content == payload, "binary body must survive the cache round-trip"
    assert downstream.calls == 1
