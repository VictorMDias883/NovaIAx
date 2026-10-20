"""
Integration tests for the operator console (:mod:`app.api.admin`).

These exercise the panel through FastAPI's :class:`TestClient`, covering:

1. The separate, opaque-cookie login flow and its CSRF protection.
2. Redirect of unauthenticated requests back to the login page.
3. Session lifecycle (logout revocation and absolute expiry).
4. User promotion / demotion / deletion, with an audit entry per action.
5. System-prompt CRUD and community-report moderation.
6. The audit viewer, optional TOTP, the enabled flag and the IP allowlist.

Admins are created by promoting a registered user directly in the database
(the panel has no endpoint to grant the first admin).  All users created by
these tests are removed afterwards so the shared ``test.db`` stays
deterministic across runs.
"""

import re
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import SessionLocal
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.admin_session import AdminSession
from app.models.user import User, UserRole
from app.utils.totp import current_code, generate_secret
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

_COOKIE = "novaiax_admin_panel"
_PASSWORD = "Password123"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a :class:`TestClient` for the whole app (lifespan included)."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def created_emails() -> AsyncGenerator[list[str], None]:
    emails: list[str] = []
    yield emails
    if emails:
        async with SessionLocal() as session:
            await session.execute(delete(User).where(User.email.in_(emails)))
            await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "csrf token not found in page"
    return match.group(1)


def _register(client: TestClient, email: str, full_name: str = "Panel User") -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"full_name": full_name, "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    assert r.status_code == 201, r.text


async def _set_role(email: str, role: UserRole) -> None:
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()


async def _set_totp(email: str, secret: str | None) -> None:
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(admin_totp_secret=secret))
        await session.commit()


async def _role_of(email: str) -> UserRole:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalars().one().role


async def _exists(email: str) -> bool:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalars().first() is not None


def _login(client: TestClient, email: str, password: str = _PASSWORD, totp_code: str = ""):
    """POST the login form with a freshly minted pre-auth CSRF token."""
    page = client.get("/admin/login")
    token = _csrf_from(page.text)
    data = {"email": email, "password": password, "csrf_token": token, "totp_code": totp_code}
    return client.post("/admin/login", data=data, follow_redirects=False)


async def _admin_login(client: TestClient, email: str) -> TestClient:
    """Promote ``email`` and log in, returning the authenticated client."""
    await _set_role(email, UserRole.ADMIN)
    r = _login(client, email)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/admin/"
    assert _COOKIE in client.cookies
    return client


def _panel_csrf(client: TestClient) -> str:
    """Return the session CSRF token from an authenticated page."""
    page = client.get("/admin/users")
    assert page.status_code == 200, page.text
    return _csrf_from(page.text)


async def _user_id(email: str) -> int:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return int(result.scalars().one().id)


async def _prompt_id(tipo: str) -> int | None:
    from app.models.system_prompt import SystemPrompt

    async with SessionLocal() as session:
        result = await session.execute(select(SystemPrompt.id).where(SystemPrompt.tipo == tipo))
        return result.scalars().first()


# ---------------------------------------------------------------------------
# Login / authentication
# ---------------------------------------------------------------------------


def test_login_page_renders(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "NovaIAx Console" in r.text
    assert "password" in r.text.lower()


def test_login_page_mints_csrf_cookie(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert "novaiax_admin_login_csrf" in client.cookies


def test_protected_pages_redirect_unauthenticated_users(client: TestClient) -> None:
    for path in ("/admin/", "/admin/users", "/admin/system-prompts", "/admin/audit"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/admin/login", path


@pytest.mark.asyncio
async def test_admin_login_sets_cookie(created_emails: list[str], client: TestClient) -> None:
    email = "panel-admin@example.com"
    created_emails.append(email)
    _register(client, email)
    await _admin_login(client, email)
    assert client.get("/admin/").status_code == 200


@pytest.mark.asyncio
async def test_login_without_csrf_is_rejected(created_emails: list[str], client: TestClient) -> None:
    email = "panel-nocsrf@example.com"
    created_emails.append(email)
    _register(client, email)
    await _set_role(email, UserRole.ADMIN)
    r = client.post("/admin/login", data={"email": email, "password": _PASSWORD}, follow_redirects=False)
    assert r.status_code == 403
    assert _COOKIE not in client.cookies


@pytest.mark.asyncio
async def test_non_admin_login_is_denied(created_emails: list[str], client: TestClient) -> None:
    email = "panel-user@example.com"
    created_emails.append(email)
    _register(client, email)
    r = _login(client, email)
    assert r.status_code == 401
    assert "E-mail ou senha inválidos" in r.text
    assert _COOKIE not in client.cookies


def test_invalid_credentials_show_error(client: TestClient) -> None:
    r = _login(client, "nobody@example.com", password="WrongPass1")
    assert r.status_code == 401
    assert "E-mail ou senha inválidos" in r.text
    assert _COOKIE not in client.cookies


def test_login_lockout_after_repeated_failures(client: TestClient) -> None:
    for _ in range(5):
        assert _login(client, "bruteforce@example.com", password="WrongPass1").status_code == 401
    locked = _login(client, "bruteforce@example.com", password="WrongPass1")
    assert locked.status_code == 429


@pytest.mark.asyncio
async def test_logout_revokes_session_and_reprotects(created_emails: list[str], client: TestClient) -> None:
    email = "panel-logout@example.com"
    created_emails.append(email)
    _register(client, email)
    await _admin_login(client, email)

    r = client.post("/admin/logout", data={"csrf_token": _panel_csrf(client)}, follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_expired_session_forces_login(created_emails: list[str], client: TestClient) -> None:
    email = "panel-expired@example.com"
    created_emails.append(email)
    _register(client, email)
    await _admin_login(client, email)
    user_id = await _user_id(email)

    async with SessionLocal() as session:
        await session.execute(
            update(AdminSession)
            .where(AdminSession.user_id == user_id)
            .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await session.commit()

    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


# ---------------------------------------------------------------------------
# CSRF on mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csrf_required_on_mutation(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-csrf@example.com"
    target_email = "panel-csrf-target@example.com"
    created_emails.extend([admin_email, target_email])
    _register(client, admin_email)
    _register(client, target_email)
    await _admin_login(client, admin_email)
    target_id = await _user_id(target_email)

    r = client.post(f"/admin/users/{target_id}/promote", follow_redirects=False)
    assert r.status_code == 403
    assert await _role_of(target_email) == UserRole.USER


# ---------------------------------------------------------------------------
# Dashboard / users / audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_and_users_list(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-dash@example.com"
    user_email = "panel-dash-user@example.com"
    created_emails.extend([admin_email, user_email])
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
async def test_promote_and_demote_user_writes_audit(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-op@example.com"
    target_email = "panel-target@example.com"
    created_emails.extend([admin_email, target_email])
    _register(client, admin_email, "Operator Admin")
    _register(client, target_email, "Target User")
    client = await _admin_login(client, admin_email)
    target_id = await _user_id(target_email)
    csrf = _panel_csrf(client)

    r = client.post(f"/admin/users/{target_id}/promote", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert await _role_of(target_email) == UserRole.ADMIN

    r = client.post(f"/admin/users/{target_id}/demote", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert await _role_of(target_email) == UserRole.USER

    async with SessionLocal() as session:
        result = await session.execute(select(AdminAuditLog).where(AdminAuditLog.action == "user.promote"))
        entry = result.scalars().first()
    assert entry is not None
    assert entry.entity == "user"
    assert entry.entity_id == str(target_id)
    assert entry.admin_email == admin_email


@pytest.mark.asyncio
async def test_delete_user(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-del-admin@example.com"
    target_email = "panel-del-target@example.com"
    created_emails.extend([admin_email, target_email])
    _register(client, admin_email, "Operator Admin")
    _register(client, target_email, "Doomed User")
    client = await _admin_login(client, admin_email)
    target_id = await _user_id(target_email)

    r = client.post(
        f"/admin/users/{target_id}/delete", data={"csrf_token": _panel_csrf(client)}, follow_redirects=False
    )
    assert r.status_code == 303
    assert not await _exists(target_email)


@pytest.mark.asyncio
async def test_audit_page_lists_entries(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-audit@example.com"
    target_email = "panel-audit-target@example.com"
    created_emails.extend([admin_email, target_email])
    _register(client, admin_email)
    _register(client, target_email)
    client = await _admin_login(client, admin_email)
    target_id = await _user_id(target_email)

    client.post(f"/admin/users/{target_id}/promote", data={"csrf_token": _panel_csrf(client)})

    page = client.get("/admin/audit")
    assert page.status_code == 200
    assert "user.promote" in page.text


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_crud_flow(created_emails: list[str], client: TestClient) -> None:
    admin_email = "panel-prompt@example.com"
    created_emails.append(admin_email)
    _register(client, admin_email, "Prompt Admin")
    client = await _admin_login(client, admin_email)
    csrf = _panel_csrf(client)

    create = client.post(
        "/admin/system-prompts",
        data={"tipo": "test-tipo", "system_prompt": "Be concise.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert create.status_code == 303

    page = client.get("/admin/system-prompts")
    assert page.status_code == 200
    assert "test-tipo" in page.text

    prompt_id = await _prompt_id("test-tipo")

    edit = client.get(f"/admin/system-prompts/{prompt_id}/edit")
    assert edit.status_code == 200

    update_r = client.post(
        f"/admin/system-prompts/{prompt_id}",
        data={"tipo": "test-tipo", "system_prompt": "Be very concise.", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert update_r.status_code == 303
    assert "Be very concise." in client.get("/admin/system-prompts").text

    delete_r = client.post(f"/admin/system-prompts/{prompt_id}/delete", data={"csrf_token": csrf}, follow_redirects=False)
    assert delete_r.status_code == 303
    assert await _prompt_id("test-tipo") is None


# ---------------------------------------------------------------------------
# Community report moderation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_moderation_queue_and_status_change(
    created_emails: list[str], client: TestClient
) -> None:
    admin_email = "panel-mod-admin@example.com"
    author_email = "panel-mod-author@example.com"
    created_emails.extend([admin_email, author_email])
    _register(client, admin_email, "Moderation Admin")
    author_tokens = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Report Author",
            "email": author_email,
            "password": _PASSWORD,
            "birth_date": "1990-01-01",
        },
    ).json()
    author_headers = {"Authorization": f"Bearer {author_tokens['access_token']}"}

    created = client.post(
        "/api/v1/community/reports",
        json={"category": "infrastructure", "title": "Buraco na rua", "description": "Buraco grande."},
        headers=author_headers,
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["id"]

    client = await _admin_login(client, admin_email)

    queue = client.get("/admin/reports")
    assert queue.status_code == 200
    assert "Buraco na rua" in queue.text

    change = client.post(
        f"/admin/reports/{report_id}/status",
        data={"status": "removed", "csrf_token": _panel_csrf(client)},
        follow_redirects=False,
    )
    assert change.status_code == 303
    assert "Buraco na rua" in client.get("/admin/reports?status=removed").text

    bad = client.post(
        f"/admin/reports/{report_id}/status",
        data={"status": "nonsense", "csrf_token": _panel_csrf(client)},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "error=" in bad.headers["location"]

    assert client.delete(f"/api/v1/community/reports/{report_id}", headers=author_headers).status_code == 204


# ---------------------------------------------------------------------------
# Optional TOTP, enabled flag and IP allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_totp_enforced_when_configured(
    monkeypatch: pytest.MonkeyPatch, created_emails: list[str], client: TestClient
) -> None:
    import app.api.admin as admin_module
    import app.services.admin_auth_service as auth_module
    from app.core.config import get_settings

    settings = get_settings().model_copy(update={"admin_require_totp": True})
    monkeypatch.setattr(admin_module, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)

    email = "panel-totp@example.com"
    created_emails.append(email)
    _register(client, email)
    secret = generate_secret()
    await _set_role(email, UserRole.ADMIN)
    await _set_totp(email, secret)

    without_code = _login(client, email)
    assert without_code.status_code == 401

    with_code = _login(client, email, totp_code=current_code(secret))
    assert with_code.status_code == 303


def test_panel_disabled_returns_404(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import app.api.admin as admin_module
    from app.core.config import get_settings

    disabled = get_settings().model_copy(update={"admin_panel_enabled": False})
    monkeypatch.setattr(admin_module, "get_settings", lambda: disabled)

    assert client.get("/admin/login").status_code == 404


def test_ip_allowlist_blocks_unknown_ip(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    import app.api.admin as admin_module
    from app.core.config import get_settings

    restricted = get_settings().model_copy(update={"admin_allowed_ips_raw": "203.0.113.9"})
    monkeypatch.setattr(admin_module, "get_settings", lambda: restricted)

    assert client.get("/admin/login").status_code == 403
