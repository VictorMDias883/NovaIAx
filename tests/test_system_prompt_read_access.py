"""
Tests for system-prompt read access on the v1 API.

``GET /api/v1/system-prompts/`` and ``GET /api/v1/system-prompts/{id}`` are
read-only and must be available to any authenticated user (any role), but the
prompt text is the agent's own instruction set: **admins** receive it, every
other caller receives the record metadata only (``id``, ``tipo``,
``created_at``).  The mutating endpoints (POST/PUT/DELETE) must remain
ADMIN-only.

Uses the full ASGI app (``app.main``) through :class:`TestClient`, so the
middleware stack and the auth dependencies are exercised end to end.
"""

import pytest
from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.main import app
from app.models.system_prompt import SystemPrompt
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update


@pytest.fixture
def client() -> TestClient:
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


@pytest.fixture
async def created_prompt_ids() -> list[int]:
    ids: list[int] = []
    yield ids
    if ids:
        async with SessionLocal() as session:
            await session.execute(delete(SystemPrompt).where(SystemPrompt.id.in_(ids)))
            await session.commit()


async def _register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Prompt Reader", "email": email, "password": "Password123", "birth_date": "1990-01-01"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _auth_headers(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _create_prompt(tipo: str, content: str) -> int:
    async with SessionLocal() as session:
        prompt = SystemPrompt(tipo=tipo, system_prompt=content)
        session.add(prompt)
        await session.commit()
        await session.refresh(prompt)
        return int(prompt.id)


async def _read_prompt(prompt_id: int) -> str | None:
    async with SessionLocal() as session:
        result = await session.execute(select(SystemPrompt).where(SystemPrompt.id == prompt_id))
        prompt = result.scalars().first()
        return prompt.system_prompt if prompt else None


@pytest.mark.asyncio
async def test_regular_user_can_list_prompts(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    prompt_id = await _create_prompt("reader-tipo", "Agent instructions")
    created_prompt_ids.append(prompt_id)

    email = "prompt-reader@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)

    r = client.get("/api/v1/system-prompts/", headers=_auth_headers(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["page"] == 1
    assert body["limit"] == 50
    assert body["total"] >= 1
    assert any(item["tipo"] == "reader-tipo" for item in body["prompts"])


@pytest.mark.asyncio
async def test_regular_user_list_omits_the_prompt_text(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    """A non-admin list response must not leak any prompt text."""
    prompt_id = await _create_prompt("reader-secret-list", "SECRET AGENT INSTRUCTIONS")
    created_prompt_ids.append(prompt_id)

    email = "prompt-reader-list@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)

    r = client.get("/api/v1/system-prompts/", headers=_auth_headers(tokens))
    assert r.status_code == 200, r.text
    items = r.json()["prompts"]
    assert items, "the list must still be populated"

    expected_fields = {
        "id",
        "tipo",
        "active",
        "sort_order",
        "icon",
        "description",
        "min_plan",
        "audience",
        "created_at",
    }
    for item in items:
        assert set(item) == expected_fields, f"unexpected fields: {sorted(item)}"
    assert "SECRET AGENT INSTRUCTIONS" not in r.text
    target = next(item for item in items if item["tipo"] == "reader-secret-list")
    assert target["id"] == prompt_id
    assert target["tipo"] == "reader-secret-list"
    assert "system_prompt" not in target


@pytest.mark.asyncio
async def test_regular_user_can_get_prompt_by_id(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    prompt_id = await _create_prompt("reader-by-id", "Be concise.")
    created_prompt_ids.append(prompt_id)

    email = "prompt-reader-id@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)

    r = client.get(f"/api/v1/system-prompts/{prompt_id}", headers=_auth_headers(tokens))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == prompt_id
    assert body["tipo"] == "reader-by-id"
    assert "created_at" in body
    # The metadata read succeeds, but the instruction text stays hidden.
    assert "system_prompt" not in body
    assert "Be concise." not in r.text


@pytest.mark.asyncio
async def test_admin_receives_the_full_prompt_text(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    """Admins keep the full payload on both read endpoints."""
    prompt_id = await _create_prompt("admin-visible", "ADMIN ONLY INSTRUCTIONS")
    created_prompt_ids.append(prompt_id)

    email = "prompt-admin@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)
    await _set_role(email, UserRole.ADMIN)
    admin_headers = _auth_headers(
        {"access_token": create_access_token(subject=str(tokens["user"]["id"]), role=UserRole.ADMIN.value),
         "refresh_token": ""}
    )

    one = client.get(f"/api/v1/system-prompts/{prompt_id}", headers=admin_headers)
    assert one.status_code == 200, one.text
    assert one.json()["system_prompt"] == "ADMIN ONLY INSTRUCTIONS"

    listed = client.get("/api/v1/system-prompts/", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    match = next(item for item in listed.json()["prompts"] if item["id"] == prompt_id)
    assert match["system_prompt"] == "ADMIN ONLY INSTRUCTIONS"


@pytest.mark.asyncio
async def test_prompt_text_is_absent_from_the_openapi_summary_schema() -> None:
    """The non-admin shape must be a distinct schema without the text field."""
    from app.schemas.system_prompt_schemas import (
        SystemPromptResponse,
        SystemPromptSummaryResponse,
    )

    metadata_fields = {"id", "tipo", "active", "sort_order", "icon", "description", "min_plan", "audience", "created_at"}
    assert set(SystemPromptResponse.model_fields) == metadata_fields | {"system_prompt"}
    assert set(SystemPromptSummaryResponse.model_fields) == metadata_fields


@pytest.mark.asyncio
async def test_read_requires_authentication(client: TestClient, created_prompt_ids: list[int]) -> None:
    prompt_id = await _create_prompt("reader-auth", "Protected content")
    created_prompt_ids.append(prompt_id)

    assert client.get("/api/v1/system-prompts/").status_code == 401
    assert client.get(f"/api/v1/system-prompts/{prompt_id}").status_code == 401


@pytest.mark.asyncio
async def test_mutating_endpoints_stay_admin_only(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    prompt_id = await _create_prompt("reader-write", "To be edited by an admin only")
    created_prompt_ids.append(prompt_id)

    email = "prompt-writer@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)

    headers = _auth_headers(tokens)
    assert (
        client.post("/api/v1/system-prompts/", json={"tipo": "x", "system_prompt": "y"}, headers=headers).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/system-prompts/{prompt_id}",
            json={"tipo": "reader-write", "system_prompt": "nope"},
            headers=headers,
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/system-prompts/{prompt_id}", headers=headers).status_code == 403

    assert await _read_prompt(prompt_id) == "To be edited by an admin only"


async def _set_role(email: str, role: UserRole) -> None:
    async with SessionLocal() as session:
        await session.execute(update(User).where(User.email == email).values(role=role))
        await session.commit()


@pytest.mark.asyncio
async def test_demoted_admin_loses_write_access_immediately(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    """A token carrying an ADMIN role claim must stop working once the DB
    role is demoted — writes reload the role from the DB (require_admin_db_role)."""
    email = "prompt-demoted@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)
    user_id = tokens["user"]["id"]

    await _set_role(email, UserRole.ADMIN)

    admin_token = create_access_token(subject=str(user_id), role=UserRole.ADMIN.value)
    admin_headers = _auth_headers({"access_token": admin_token, "refresh_token": ""})

    create = client.post(
        "/api/v1/system-prompts/", json={"tipo": "demoted-tipo", "system_prompt": "fresh"}, headers=admin_headers
    )
    assert create.status_code == 201, create.text
    created_prompt_ids.append(create.json()["id"])

    target_id = await _create_prompt("demoted-target", "unchanged")
    created_prompt_ids.append(target_id)

    await _set_role(email, UserRole.USER)

    assert (
        client.post(
            "/api/v1/system-prompts/", json={"tipo": "x", "system_prompt": "y"}, headers=admin_headers
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/system-prompts/{target_id}",
            json={"tipo": "demoted-target", "system_prompt": "nope"},
            headers=admin_headers,
        ).status_code
        == 403
    )
    assert client.delete(f"/api/v1/system-prompts/{target_id}", headers=admin_headers).status_code == 403

    assert await _read_prompt(target_id) == "unchanged"

    assert client.get("/api/v1/system-prompts/", headers=admin_headers).status_code == 200
    assert client.get(f"/api/v1/system-prompts/{target_id}", headers=admin_headers).status_code == 200


@pytest.mark.asyncio
async def test_redaction_follows_the_token_role_claim_not_the_database(
    client: TestClient, created_emails: list[str], created_prompt_ids: list[int]
) -> None:
    """Documented gap: reads decide entitlement from the access-token role claim.

    Writes reload the role from the database (``require_admin_db_role``) so a
    demotion bites immediately, but the read endpoints authenticate with
    ``get_current_user`` only.  An admin demoted mid-session therefore keeps
    receiving the prompt text until their access token expires.  This test
    pins that behaviour so the asymmetry is visible and easy to tighten later
    (e.g. by resolving the live role for the read path too).
    """
    prompt_id = await _create_prompt("demoted-read", "STILL VISIBLE AFTER DEMOTION")
    created_prompt_ids.append(prompt_id)

    email = "prompt-demoted-read@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)
    user_id = tokens["user"]["id"]
    await _set_role(email, UserRole.ADMIN)

    stale_admin_headers = _auth_headers(
        {"access_token": create_access_token(subject=str(user_id), role=UserRole.ADMIN.value),
         "refresh_token": ""}
    )
    await _set_role(email, UserRole.USER)

    r = client.get(f"/api/v1/system-prompts/{prompt_id}", headers=stale_admin_headers)
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "STILL VISIBLE AFTER DEMOTION"


@pytest.mark.asyncio
async def test_deleted_admin_loses_write_access_immediately(client: TestClient, created_emails: list[str]) -> None:
    """A token issued to a now-deleted user (still claiming ADMIN) is rejected
    outright, because the dependency reloads the user from the DB."""
    email = "prompt-deleted@example.com"
    created_emails.append(email)
    tokens = await _register(client, email)
    user_id = tokens["user"]["id"]

    await _set_role(email, UserRole.ADMIN)
    admin_headers = _auth_headers(
        {"access_token": create_access_token(subject=str(user_id), role=UserRole.ADMIN.value), "refresh_token": ""}
    )

    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.email == email))
        await session.commit()

    post = client.post("/api/v1/system-prompts/", json={"tipo": "x", "system_prompt": "y"}, headers=admin_headers)
    assert post.status_code == 401

    get = client.get("/api/v1/system-prompts/", headers=admin_headers)
    assert get.status_code == 401
