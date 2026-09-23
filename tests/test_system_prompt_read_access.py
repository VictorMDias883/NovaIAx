"""
Tests for system-prompt read access on the v1 API.

``GET /api/v1/system-prompts/`` and ``GET /api/v1/system-prompts/{id}`` are
read-only and must be available to any authenticated user (any role).  The
mutating endpoints (POST/PUT/DELETE) must remain ADMIN-only.

Uses the full ASGI app (``app.main``) through :class:`TestClient`, so the
middleware stack and the auth dependencies are exercised end to end.
"""

import pytest
from app.db.session import SessionLocal
from app.main import app
from app.models.system_prompt import SystemPrompt
from app.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import delete, select


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
        json={"full_name": "Prompt Reader", "email": email, "password": "Password123"},
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
    assert body["system_prompt"] == "Be concise."
    assert "created_at" in body


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
