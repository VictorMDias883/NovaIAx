"""HTTP-level navigation tests for the objective/roadmap endpoints."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import SessionLocal
from app.main import app
from app.models.objective import Objective
from fastapi.testclient import TestClient


class StubGroq:
    """Groq stand-in: returns a full 7-day JSON payload for objective creation."""

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        days = ", ".join(f'{{"day": {i}, "meta": "meta {i}"}}' for i in range(1, 8))
        return f'{{"days": [{days}]}}'

    async def create_chat_completion_with_history(self, system_prompt: str, messages: list[dict[str, str]]) -> str:
        return "resposta simulada"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr("app.api.v1.objective_router.GroqAIClient", StubGroq)

    with TestClient(app) as c:
        yield c


def _register_user(client: TestClient, email: str) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "User",
            "email": email,
            "password": "Password123",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def _due_date() -> str:
    return "2027-12-31T00:00:00Z"


def test_e2e_register_list_and_days(client: TestClient) -> None:
    user = _register_user(client, _unique_email("owner"))
    token = user["access_token"]

    created = client.post(
        "/api/v1/objectives/register",
        json={
            "title": "Aprender Python",
            "description": "Dominar a linguagem",
            "due_date": _due_date(),
        },
        headers=_auth(token),
    )
    assert created.status_code == 200, created.text
    objective_id = created.json()["id"]
    assert len(created.json()["days"]) == 7

    listed = client.get("/api/v1/objectives/", headers=_auth(token))
    assert listed.status_code == 200
    assert any(item["id"] == objective_id for item in listed.json())

    days = client.get(f"/api/v1/objectives/{objective_id}/roadmap/days", headers=_auth(token))
    assert days.status_code == 200
    assert len(days.json()["days"]) == 7


def test_another_user_cannot_access_objective(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner2"))
    other = _register_user(client, _unique_email("other"))

    created = client.post(
        "/api/v1/objectives/register",
        json={
            "title": "Meta privada",
            "description": None,
            "due_date": _due_date(),
        },
        headers=_auth(owner["access_token"]),
    )
    assert created.status_code == 200
    objective_id = created.json()["id"]
    day_id = created.json()["days"][0]["id"]

    other_token = _auth(other["access_token"])

    listed = client.get("/api/v1/objectives/", headers=other_token)
    assert all(item["id"] != objective_id for item in listed.json())

    days = client.get(f"/api/v1/objectives/{objective_id}/roadmap/days", headers=other_token)
    assert days.status_code == 403

    patch = client.patch(
        f"/api/v1/objectives/{objective_id}/roadmap/days/{day_id}",
        json={"status": "COMPLETED"},
        headers=other_token,
    )
    assert patch.status_code == 403

    renew = client.post(f"/api/v1/objectives/{objective_id}/roadmap/renew", headers=other_token)
    assert renew.status_code == 403


def test_missing_objective_and_day_are_404(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner3"))
    token = _auth(owner["access_token"])

    days = client.get("/api/v1/objectives/999999/roadmap/days", headers=token)
    assert days.status_code == 404

    patch = client.patch(
        "/api/v1/objectives/999999/roadmap/days/999999",
        json={"status": "COMPLETED"},
        headers=token,
    )
    assert patch.status_code == 404


def test_other_objectives_day_returns_404(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner4"))
    token = _auth(owner["access_token"])

    obj_a = client.post(
        "/api/v1/objectives/register",
        json={"title": "Meta A", "description": None, "due_date": _due_date()},
        headers=token,
    ).json()
    obj_b = client.post(
        "/api/v1/objectives/register",
        json={"title": "Meta B", "description": None, "due_date": _due_date()},
        headers=token,
    ).json()
    day_b = obj_b["days"][0]["id"]

    resp = client.patch(
        f"/api/v1/objectives/{obj_a['id']}/roadmap/days/{day_b}",
        json={"status": "COMPLETED"},
        headers=token,
    )
    assert resp.status_code == 404


def test_invalid_day_status_returns_422(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner5"))
    created = client.post(
        "/api/v1/objectives/register",
        json={"title": "Meta A", "description": None, "due_date": _due_date()},
        headers=_auth(owner["access_token"]),
    ).json()
    day_id = created["days"][0]["id"]

    resp = client.patch(
        f"/api/v1/objectives/{created['id']}/roadmap/days/{day_id}",
        json={"status": "NOT_A_STATUS"},
        headers=_auth(owner["access_token"]),
    )
    assert resp.status_code == 422


def test_empty_assistant_message_returns_422(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner5b"))

    resp = client.post(
        "/api/v1/objectives/assistant",
        json={"user_message": ""},
        headers=_auth(owner["access_token"]),
    )
    assert resp.status_code == 422


def test_empty_roadmap_returns_empty_days(client: TestClient) -> None:
    """An objective seeded without roadmap days must navigate cleanly."""
    user = _register_user(client, _unique_email("owner6"))
    token = _auth(user["access_token"])
    user_id = int(user["user"]["id"])

    async def seed() -> int:
        async with SessionLocal() as session:
            objective = Objective(
                title="Sem dias",
                description=None,
                due_date=datetime(2027, 12, 31, tzinfo=UTC),
                user_id=user_id,
            )
            session.add(objective)
            await session.commit()
            await session.refresh(objective)
            return objective.id

    objective_id = asyncio.run(seed())

    days = client.get(f"/api/v1/objectives/{objective_id}/roadmap/days", headers=token)
    assert days.status_code == 200
    assert days.json()["days"] == []

    listed = client.get("/api/v1/objectives/", headers=token)
    target = next(item for item in listed.json() if item["id"] == objective_id)
    assert target["days"] == []


def test_multiple_roadmaps_same_user(client: TestClient) -> None:
    owner = _register_user(client, _unique_email("owner7"))
    token = _auth(owner["access_token"])

    first = client.post(
        "/api/v1/objectives/register",
        json={"title": "Um", "description": None, "due_date": _due_date()},
        headers=token,
    )
    second = client.post(
        "/api/v1/objectives/register",
        json={"title": "Dois", "description": None, "due_date": _due_date()},
        headers=token,
    )
    assert first.status_code == 200 and second.status_code == 200

    listed = client.get("/api/v1/objectives/", headers=token).json()
    ids = [item["id"] for item in listed]
    assert first.json()["id"] in ids
    assert second.json()["id"] in ids

    days_first = client.get(f"/api/v1/objectives/{first.json()['id']}/roadmap/days", headers=token).json()
    days_second = client.get(f"/api/v1/objectives/{second.json()['id']}/roadmap/days", headers=token).json()
    assert all(d["objective_id"] == first.json()["id"] for d in days_first["days"])
    assert all(d["objective_id"] == second.json()["id"] for d in days_second["days"])
