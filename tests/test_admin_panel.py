"""
Integration tests for the server-rendered admin panel (:mod:`app.api.admin`).

These tests exercise the panel through FastAPI's :class:`TestClient`
(same in-process ASGI app as the API tests), covering:

1. Login page rendering and the ADMIN-only cookie session flow.
2. Redirect of unauthenticated users back to the login page.
3. User promotion / demotion / deletion.
4. System-prompt create / read / update / delete.

Users are registered through the public auth API and promoted to ADMIN
directly in the database (the panel has no endpoint that grants the
*first* admin, and the startup bootstrap always creates
``admin@admin.com`` with an unknown random password).

All users created by these tests are removed afterwards so the shared
``test.db`` stays deterministic across runs.
"""

import pytest
from app.db.session import SessionLocal
from app.main import app
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

_COOKIE = "novaiax_admin_session"
_PASSWORD = "Password123"


@pytest.fixture
def client() -> TestClient:
    """Provide a :class:`TestClient` for the whole app (lifespan included)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def created_emails() -> list[str]:
    emails: list[str] = []
    yield emails
    if emails:
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email.in_(emails)))
            await session.commit()


def _register(client: TestClient, email: str, full_name: str = "Panel User") -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"full_name": full_name, "email": email, "password": _PASSWORD},
    )
    assert r.status_code == 201, r.text


async def _set_role(email: str, role: UserRole) -> None:
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()


async def _role_of(email: str) -> UserRole:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().one()
        return user.role


async def _exists(email: str) -> bool:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalars().first() is not None


async def _admin_login(client: TestClient, email: str) -> TestClient:
    """Log in to the panel as an admin and return a client holding the session cookie."""
    await _set_role(email, UserRole.ADMIN)
    r = client.post(
        "/admin/login",
        data={"email": email, "password": _PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin/"
    assert _COOKIE in client.cookies
    return client


def _track(client: TestClient, created_emails: list[str], email: str) -> None:
    created_emails.append(email)


# ---------------------------------------------------------------------------
# Login / authentication
# ---------------------------------------------------------------------------


def test_login_page_renders(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "NovaIAx Admin" in r.text
    assert "password" in r.text.lower()


def test_protected_pages_redirect_unauthenticated_users(client: TestClient) -> None:
    for path in ("/admin/", "/admin/users", "/admin/system-prompts", "/admin/system-prompts/1/edit"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/admin/login", path


@pytest.mark.asyncio
async def test_admin_login_sets_cookie(created_emails: list[str], client: TestClient) -> None:
    email = "panel-admin@example.com"
    _track(client, created_emails, email)
    _register(client, email)
    await _admin_login(client, email)
    body = client.post(
        "/admin/login",
        data={"email": email, "password": _PASSWORD},
        follow_redirects=False,
    )
    assert body.status_code == 303
    assert body.headers["location"] == "/admin/"


@pytest.mark.asyncio
async def test_non_admin_login_is_denied(created_emails: list[str], client: TestClient) -> None:
    email = "panel-user@example.com"
    _track(client, created_emails, email)
    _register(client, email)
    r = client.post("/admin/login", data={"email": email, "password": _PASSWORD})
    assert r.status_code == 403
    assert "Only administrators" in r.text
    assert _COOKIE not in client.cookies


def test_invalid_credentials_show_error(client: TestClient) -> None:
    r = client.post(
        "/admin/login",
        data={"email": "nobody@example.com", "password": "WrongPass1"},
    )
    assert r.status_code == 401
    assert "Invalid email or password" in r.text
    assert _COOKIE not in client.cookies


@pytest.mark.asyncio
async def test_logout_clears_session_and_reprotects(created_emails: list[str], client: TestClient) -> None:
    email = "panel-logout@example.com"
    _track(client, created_emails, email)
    _register(client, email)
    await _admin_login(client, email)

    r = client.post("/admin/logout", follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# Dashboard / users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_and_users_list(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-dash@example.com"
    user_email = "panel-dash-user@example.com"
    _track(client, created_emails, admin_email)
    _track(client, created_emails, user_email)
    _register(client, admin_email, "Dashboard Admin")
    _register(client, user_email, "Dashboard Regular")
    await _admin_login(client, admin_email)

    dash = client.get("/admin/")
    assert dash.status_code == 200
    assert "Dashboard" in dash.text

    users = client.get("/admin/users")
    assert users.status_code == 200
    assert admin_email in users.text
    assert user_email in users.text


@pytest.mark.asyncio
async def test_promote_and_demote_user(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-op@example.com"
    target_email = "panel-target@example.com"
    _track(client, created_emails, admin_email)
    _track(client, created_emails, target_email)
    _register(client, admin_email, "Operator Admin")
    _register(client, target_email, "Target User")
    client = await _admin_login(client, admin_email)

    target_id = await _user_id(target_email)

    r = client.post(f"/admin/users/{target_id}/promote", follow_redirects=False)
    assert r.status_code == 303
    assert await _role_of(target_email) == UserRole.ADMIN

    r = client.post(f"/admin/users/{target_id}/demote", follow_redirects=False)
    assert r.status_code == 303
    assert await _role_of(target_email) == UserRole.USER


@pytest.mark.asyncio
async def test_delete_user(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-del-admin@example.com"
    target_email = "panel-del-target@example.com"
    _track(client, created_emails, admin_email)
    _track(client, created_emails, target_email)
    _register(client, admin_email, "Operator Admin")
    _register(client, target_email, "Doomed User")
    client = await _admin_login(client, admin_email)

    target_id = await _user_id(target_email)
    r = client.post(f"/admin/users/{target_id}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert not await _exists(target_email)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_crud_flow(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-prompt@example.com"
    _track(client, created_emails, admin_email)
    _register(client, admin_email, "Prompt Admin")
    client = await _admin_login(client, admin_email)

    create = client.post(
        "/admin/system-prompts",
        data={"tipo": "test-tipo", "system_prompt": "Be concise."},
        follow_redirects=False,
    )
    assert create.status_code == 303

    page = client.get("/admin/system-prompts")
    assert page.status_code == 200
    assert "test-tipo" in page.text

    prompt_id = await _prompt_id("test-tipo")

    edit = client.get(f"/admin/system-prompts/{prompt_id}/edit")
    assert edit.status_code == 200
    assert edit.request.url.path == f"/admin/system-prompts/{prompt_id}/edit"

    update_r = client.post(
        f"/admin/system-prompts/{prompt_id}",
        data={"tipo": "test-tipo", "system_prompt": "Be very concise."},
        follow_redirects=False,
    )
    assert update_r.status_code == 303

    page = client.get("/admin/system-prompts")
    assert "Be very concise." in page.text

    delete_r = client.post(
        f"/admin/system-prompts/{prompt_id}/delete",
        follow_redirects=False,
    )
    assert delete_r.status_code == 303
    assert await _prompt_id("test-tipo") is None


async def _user_id(email: str) -> int:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalars().one()
        return int(user.id)


async def _prompt_id(tipo: str) -> int | None:
    from app.models.system_prompt import SystemPrompt

    async with SessionLocal() as session:
        result = await session.execute(select(SystemPrompt.id).where(SystemPrompt.tipo == tipo))
        return result.scalars().first()
