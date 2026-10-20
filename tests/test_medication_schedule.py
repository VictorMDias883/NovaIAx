"""
Tests for the structured medication-care schedule.

Covers the new ``schedule_times`` / ``start_date`` / ``end_date`` fields on
``/me/medications`` (validation, normalisation, update) and the intake body's
``scheduled_for`` alias plus timezone interpretation (naive datetimes are read
in the user's ``/me/agent`` timezone — the default is ``America/Sao_Paulo``).
"""

import uuid
from datetime import datetime

import pytest
from app.main import app
from fastapi.testclient import TestClient

_PASSWORD = "Password123"
_API = "/api/v1"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _register(client: TestClient) -> str:
    email = f"med-{uuid.uuid4().hex[:10]}@example.com"
    resp = client.post(
        f"{_API}/auth/register",
        json={
            "full_name": "Med User",
            "email": email,
            "password": _PASSWORD,
            "birth_date": "1990-01-01",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_medication(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {
        "name": "Vitamina D",
        "dosage": "1 comprimido",
        "frequency": "2x ao dia",
        "schedule_times": ["20:00", "08:00"],
        "start_date": "2026-01-01",
    }
    payload.update(overrides)
    resp = client.post(f"{_API}/me/medications", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_medication_returns_sorted_schedule(client: TestClient) -> None:
    headers = _auth(_register(client))
    body = _create_medication(client, headers)

    assert body["schedule_times"] == ["08:00", "20:00"]
    assert body["start_date"] == "2026-01-01"
    assert body["end_date"] is None
    assert body["active"] is True


def test_schedule_times_are_deduplicated(client: TestClient) -> None:
    headers = _auth(_register(client))
    body = _create_medication(client, headers, schedule_times=["08:00", "08:00", "20:00"])
    assert body["schedule_times"] == ["08:00", "20:00"]


def test_create_requires_start_date(client: TestClient) -> None:
    headers = _auth(_register(client))
    resp = client.post(
        f"{_API}/me/medications",
        json={"name": "Sem data", "schedule_times": []},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_invalid_schedule_time_is_rejected(client: TestClient) -> None:
    headers = _auth(_register(client))
    resp = client.post(
        f"{_API}/me/medications",
        json={"name": "X", "start_date": "2026-01-01", "schedule_times": ["25:00"]},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_non_zero_padded_schedule_time_is_rejected(client: TestClient) -> None:
    headers = _auth(_register(client))
    resp = client.post(
        f"{_API}/me/medications",
        json={"name": "X", "start_date": "2026-01-01", "schedule_times": ["8:00"]},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_too_many_schedule_times_is_rejected(client: TestClient) -> None:
    headers = _auth(_register(client))
    many = [f"{hour:02d}:00" for hour in range(13)]
    resp = client.post(
        f"{_API}/me/medications",
        json={"name": "X", "start_date": "2026-01-01", "schedule_times": many},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_end_date_before_start_date_is_rejected(client: TestClient) -> None:
    headers = _auth(_register(client))
    resp = client.post(
        f"{_API}/me/medications",
        json={
            "name": "X",
            "start_date": "2026-02-01",
            "end_date": "2026-01-01",
            "schedule_times": [],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_update_schedule_and_dates(client: TestClient) -> None:
    headers = _auth(_register(client))
    medication = _create_medication(client, headers)

    resp = client.patch(
        f"{_API}/me/medications/{medication['id']}",
        json={"schedule_times": ["12:30"], "end_date": "2026-03-31"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_times"] == ["12:30"]
    assert body["end_date"] == "2026-03-31"


def test_update_rejects_invalid_schedule_time(client: TestClient) -> None:
    headers = _auth(_register(client))
    medication = _create_medication(client, headers)

    resp = client.patch(
        f"{_API}/me/medications/{medication['id']}",
        json={"schedule_times": ["99:99"]},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_medication_list_includes_schedule(client: TestClient) -> None:
    headers = _auth(_register(client))
    _create_medication(client, headers)

    resp = client.get(f"{_API}/me/medications", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items and items[0]["schedule_times"] == ["08:00", "20:00"]


def test_intake_accepts_scheduled_for_and_converts_from_user_timezone(client: TestClient) -> None:
    headers = _auth(_register(client))
    medication = _create_medication(client, headers)

    resp = client.post(
        f"{_API}/me/medications/{medication['id']}/intakes",
        json={"status": "taken", "scheduled_for": "2026-01-10T08:00:00"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["medication_id"] == medication["id"]
    assert body["status"] == "taken"

    # Default profile timezone is America/Sao_Paulo (UTC-3): 08:00 local → 11:00 UTC.
    scheduled = datetime.fromisoformat(body["scheduled_at"])
    assert (scheduled.hour, scheduled.minute) == (11, 0)


def test_intake_accepts_legacy_scheduled_at_alias(client: TestClient) -> None:
    headers = _auth(_register(client))
    medication = _create_medication(client, headers)

    resp = client.post(
        f"{_API}/me/medications/{medication['id']}/intakes",
        json={"status": "skipped", "scheduled_at": "2026-01-10T11:00:00Z"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "skipped"


def test_intake_rejects_unknown_status(client: TestClient) -> None:
    headers = _auth(_register(client))
    medication = _create_medication(client, headers)

    resp = client.post(
        f"{_API}/me/medications/{medication['id']}/intakes",
        json={"status": "exploded"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
