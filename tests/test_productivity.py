"""
Tests for the personal-productivity module.

Covers tasks, reminders, notes, checklists, habits, focus sessions, the merged
agenda and today summary, plus the plan-gated AI weekly-plan flow (with a fake
AI client).  Also unit-tests the streak and suggestion-parsing helpers.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from app.api.deps import get_ai_router
from app.db.session import SessionLocal
from app.main import app
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.services.productivity_service import ProductivityService
from fastapi.testclient import TestClient

_PASSWORD = "Password123"


class FakeAI:
    """Duck-typed AI double returning a fixed weekly-plan JSON payload."""

    def __init__(self) -> None:
        self.calls = 0

    async def create_chat_completion(self, system_prompt: str, user_message: str) -> str:
        self.calls += 1
        return (
            '{"suggestions": ['
            '{"kind": "task", "title": "Planejar a semana", "due_at": "2027-01-04T09:00:00", '
            '"priority": "high", "notes": null},'
            '{"kind": "reminder", "title": "Beber água", "remind_at": "2027-01-04T08:00:00", '
            '"priority": "low", "notes": null}]}'
        )

    async def create_chat_completion_with_history(self, system_prompt: str, messages: list[dict[str, str]]) -> str:
        return await self.create_chat_completion(system_prompt, "")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    fake = FakeAI()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides})
    app.dependency_overrides[get_ai_router] = lambda: fake
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, prefix: str = "prod") -> dict:
    email = f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Prod User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _me_id(client: TestClient, token: str) -> int:
    resp = client.get("/api/v1/auth/me", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return int(resp.json()["id"])


def _grant_pro(user_id: int) -> None:
    async def _run() -> None:
        async with SessionLocal() as session:
            session.add(
                Subscription(
                    user_id=user_id,
                    plan=SubscriptionPlan.PRO,
                    status=SubscriptionStatus.ACTIVE,
                    product_id="pro_monthly",
                    purchase_token_hash="a" * 64,
                    auto_renewing=True,
                    is_trial=False,
                )
            )
            await session.commit()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
def test_task_crud_and_pagination(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    for index in range(3):
        created = client.post(
            "/api/v1/me/tasks",
            json={"title": f"Tarefa {index}", "due_at": "2027-06-01T10:00:00Z"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "pending"

    page1 = client.get("/api/v1/me/tasks?page=1&limit=2", headers=headers)
    assert page1.status_code == 200
    body = page1.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    page2 = client.get("/api/v1/me/tasks?page=2&limit=2", headers=headers)
    assert len(page2.json()["items"]) == 1

    task_id = body["items"][0]["id"]
    patched = client.patch(f"/api/v1/me/tasks/{task_id}", json={"status": "done"}, headers=headers)
    assert patched.status_code == 200
    assert patched.json()["status"] == "done"

    assert client.delete(f"/api/v1/me/tasks/{task_id}", headers=headers).status_code == 204
    assert client.get(f"/api/v1/me/tasks/{task_id}", headers=headers).status_code == 404


def test_tasks_are_owner_scoped(client: TestClient) -> None:
    token_a = _register(client, "a")["access_token"]
    token_b = _register(client, "b")["access_token"]

    created = client.post("/api/v1/me/tasks", json={"title": "Segredo"}, headers=_auth(token_a))
    task_id = created.json()["id"]

    assert client.get(f"/api/v1/me/tasks/{task_id}", headers=_auth(token_b)).status_code == 404
    assert (
        client.patch(f"/api/v1/me/tasks/{task_id}", json={"status": "done"}, headers=_auth(token_b)).status_code == 404
    )
    assert client.delete(f"/api/v1/me/tasks/{task_id}", headers=_auth(token_b)).status_code == 404


def test_recurring_task_rolls_forward_when_completed(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    created = client.post(
        "/api/v1/me/tasks",
        json={
            "title": "Pagar conta",
            "due_at": "2027-06-01T10:00:00Z",
            "recurrence": "monthly",
        },
        headers=headers,
    ).json()

    client.patch(f"/api/v1/me/tasks/{created['id']}", json={"status": "done"}, headers=headers)
    listing = client.get("/api/v1/me/tasks", headers=headers).json()
    assert listing["total"] == 2
    assert any(item["status"] == "pending" for item in listing["items"])


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
def test_reminder_create_and_snooze(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    created = client.post(
        "/api/v1/me/reminders",
        json={"title": "Consulta", "remind_at": "2027-06-01T12:00:00Z"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    reminder_id = created.json()["id"]

    snoozed = client.post(
        f"/api/v1/me/reminders/{reminder_id}/snooze",
        json={"snoozed_until": "2027-06-01T13:00:00Z"},
        headers=headers,
    )
    assert snoozed.status_code == 200
    assert snoozed.json()["snoozed_until"].startswith("2027-06-01T13:00")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
def test_note_tag_filter_and_pin(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    client.post(
        "/api/v1/me/notes",
        json={"title": "Compras", "content": "# lista", "tags": ["casa", "mercado"], "pinned": True},
        headers=headers,
    )
    client.post(
        "/api/v1/me/notes",
        json={"title": "Ideias", "content": "rascunho", "tags": ["trabalho"]},
        headers=headers,
    )

    tagged = client.get("/api/v1/me/notes?tag=casa", headers=headers).json()
    assert tagged["total"] == 1
    assert tagged["items"][0]["title"] == "Compras"

    pinned = client.get("/api/v1/me/notes?pinned=true", headers=headers).json()
    assert pinned["total"] == 1


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------
def test_checklist_items(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    checklist = client.post(
        "/api/v1/me/checklists",
        json={"title": "Viagem", "category": "viagem"},
        headers=headers,
    ).json()
    assert checklist["category"] == "viagem"

    item = client.post(
        f"/api/v1/me/checklists/{checklist['id']}/items",
        json={"text": "Passaporte"},
        headers=headers,
    ).json()

    updated = client.patch(
        f"/api/v1/me/checklists/{checklist['id']}/items/{item['id']}",
        json={"done": True},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["done"] is True

    fetched = client.get(f"/api/v1/me/checklists/{checklist['id']}", headers=headers).json()
    assert len(fetched["items"]) == 1


# ---------------------------------------------------------------------------
# Habits
# ---------------------------------------------------------------------------
def test_habit_checkin_is_idempotent_and_tracks_streak(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    habit = client.post("/api/v1/me/habits", json={"title": "Ler"}, headers=headers).json()

    first = client.post(f"/api/v1/me/habits/{habit['id']}/check-ins", json={}, headers=headers)
    assert first.status_code == 200
    assert first.json()["streak"] == 1

    # Same-day second check-in is a no-op, not a duplicate.
    second = client.post(f"/api/v1/me/habits/{habit['id']}/check-ins", json={}, headers=headers)
    assert second.json()["streak"] == 1
    assert len(second.json()["check_ins"]) == 1


def test_streak_helper() -> None:
    today = date(2027, 1, 10)
    assert ProductivityService._streak([], today) == 0
    assert ProductivityService._streak([today], today) == 1
    assert ProductivityService._streak([today, today - timedelta(days=1)], today) == 2
    # A gap breaks the streak; a pending streak ending "yesterday" still counts.
    assert ProductivityService._streak([today - timedelta(days=2)], today) == 0
    assert ProductivityService._streak([today - timedelta(days=1)], today) == 1


# ---------------------------------------------------------------------------
# Focus sessions
# ---------------------------------------------------------------------------
def test_focus_sessions(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    created = client.post(
        "/api/v1/me/focus-sessions",
        json={"label": "Estudo", "start_at": "2027-06-01T09:00:00Z"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    finished = client.patch(
        f"/api/v1/me/focus-sessions/{session_id}",
        json={"end_at": "2027-06-01T09:25:00Z"},
        headers=headers,
    )
    assert finished.status_code == 200

    listing = client.get("/api/v1/me/focus-sessions", headers=headers).json()
    assert listing["total"] == 1


# ---------------------------------------------------------------------------
# Agenda / today
# ---------------------------------------------------------------------------
def test_agenda_merges_tasks_and_reminders(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    client.post(
        "/api/v1/me/tasks",
        json={"title": "Enviar relatório", "due_at": "2027-06-02T14:00:00Z"},
        headers=headers,
    )
    client.post(
        "/api/v1/me/reminders",
        json={"title": "Tomar remédio", "remind_at": "2027-06-02T08:00:00Z"},
        headers=headers,
    )

    agenda = client.get("/api/v1/me/agenda?from=2027-06-01T00:00:00Z&to=2027-06-03T00:00:00Z", headers=headers)
    assert agenda.status_code == 200, agenda.text
    body = agenda.json()
    kinds = [item["kind"] for item in body["items"]]
    assert "task" in kinds and "reminder" in kinds
    # Chronological ordering: the 08:00 reminder precedes the 14:00 task.
    ats = [item["at"] for item in body["items"]]
    assert ats == sorted(ats)


def test_today_summary(client: TestClient) -> None:
    token = _register(client)["access_token"]
    headers = _auth(token)

    now = datetime.now(UTC)
    client.post(
        "/api/v1/me/tasks",
        json={"title": "Hoje", "due_at": now.isoformat()},
        headers=headers,
    )
    today = client.get("/api/v1/me/today", headers=headers)
    assert today.status_code == 200, today.text
    body = today.json()
    assert body["counts"]["tasks"] >= 1
    assert "date" in body and "habits" in body


# ---------------------------------------------------------------------------
# Weekly plan (plan-gated)
# ---------------------------------------------------------------------------
def test_weekly_plan_requires_automation_plan(client: TestClient) -> None:
    token = _register(client)["access_token"]
    resp = client.post("/api/v1/me/weekly-plan", json={}, headers=_auth(token))
    assert resp.status_code == 402, resp.text
    assert resp.json()["code"] == "plan_upgrade_required"


def test_weekly_plan_generate_and_accept(client: TestClient) -> None:
    registered = _register(client)
    token = registered["access_token"]
    headers = _auth(token)
    user_id = _me_id(client, token)
    _grant_pro(user_id)

    generated = client.post("/api/v1/me/weekly-plan", json={"focus": "trabalho e saúde"}, headers=headers)
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert len(body["suggestions"]) == 2
    assert body["disclaimer"]

    # Nothing is persisted before accept.
    assert client.get("/api/v1/me/tasks", headers=headers).json()["total"] == 0

    accepted = client.post("/api/v1/me/weekly-plan/accept", headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"created_tasks": 1, "created_reminders": 1}

    assert client.get("/api/v1/me/tasks", headers=headers).json()["total"] == 1
    assert client.get("/api/v1/me/reminders", headers=headers).json()["total"] == 1


def test_parse_suggestions_rejects_junk() -> None:
    assert ProductivityService._parse_suggestions({"suggestions": "not-a-list"}) == []
    parsed = ProductivityService._parse_suggestions(
        {
            "suggestions": [
                {"kind": "todo", "title": "  "},
                {"kind": "task", "title": "Ok", "priority": "urgent"},
            ]
        }
    )
    assert parsed == [
        {
            "kind": "task",
            "title": "Ok",
            "due_at": None,
            "remind_at": None,
            "priority": "medium",
            "notes": None,
        }
    ]
