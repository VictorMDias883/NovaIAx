"""
Full-module coverage tests for data export and account deletion.

The privacy suite must cover **every** feature module, not just productivity:
medication care, personal finance, study, the logbook and community.  These
tests seed one row per module (plus a community photo file on disk), then:

1. assert ``GET /me/export`` includes the new module sections and their content
   while never leaking ``password_hash``/``purchase_token_hash``;
2. assert ``DELETE /me`` removes every owned row across all modules, deletes the
   community photo file from disk, and leaves a *different* user untouched
   (including another user's confirmation that pointed at the deleted report).
"""

import asyncio
from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from app.api.v1.community_router import _read_bounded_upload
from app.db.session import SessionLocal
from app.main import app
from app.models.community import (
    CommunityReport,
    DraftComplaint,
    ReportCategory,
    ReportConfirmation,
    ReportFlag,
    ReportStatus,
    UserBlock,
)
from app.models.finance import Bill, BillStatus, Budget, Expense, Income
from app.models.health import IntakeStatus, Medication, MedicationIntake
from app.models.logbook import LogEntry, LogKind
from app.models.study import Flashcard, FlashcardDeck, FlashcardReview, StudyPlan
from app.models.user import User, UserRole
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from tests.helpers import force_memory_redis

_PASSWORD = "Password123"
_API = "/api/v1"

_REGISTER = {
    "full_name": "Coverage User",
    "password": _PASSWORD,
    "birth_date": "1990-01-01",
}


def test_upload_reader_rejects_oversize_without_reading_full_file() -> None:
    upload = UploadFile(file=BytesIO(b"x" * 100))

    with pytest.raises(HTTPException) as error:
        asyncio.run(_read_bounded_upload(upload, 8))

    assert error.value.status_code == 413


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, email: str) -> dict:
    resp = client.post(f"{_API}/auth/register", json={**_REGISTER, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_and_return_ids(user_id: int, other_id: int, photo_name: str) -> dict[str, int]:
    """Insert one representative row in every module; return key ids."""
    async with SessionLocal() as session:
        medication = Medication(
            user_id=user_id,
            name="Dipirona",
            dosage="500mg",
            frequency="daily",
            schedule_times=["08:00", "20:00"],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 12, 31),
            active=True,
        )
        session.add(medication)
        await session.flush()
        session.add(
            MedicationIntake(
                user_id=user_id,
                medication_id=medication.id,
                status=IntakeStatus.TAKEN,
            )
        )

        session.add(
            Expense(user_id=user_id, amount_cents=1234, category="food", occurred_at=datetime.now(UTC))
        )
        session.add(
            Income(user_id=user_id, amount_cents=5000, source="salary", occurred_at=datetime.now(UTC))
        )
        session.add(
            Bill(user_id=user_id, name="Luz", amount_cents=9999, due_date=date(2026, 2, 10), status=BillStatus.PENDING)
        )
        session.add(Budget(user_id=user_id, month="2026-02", category="food", amount_cents=50000))

        plan = StudyPlan(user_id=user_id, subject="Matemática")
        session.add(plan)
        deck = FlashcardDeck(user_id=user_id, name="Verbos")
        session.add(deck)
        await session.flush()
        card = Flashcard(
            user_id=user_id,
            deck_id=deck.id,
            front="to be",
            back="ser",
            due_at=datetime.now(UTC),
        )
        session.add(card)
        await session.flush()
        session.add(FlashcardReview(user_id=user_id, flashcard_id=card.id, grade=4))

        session.add(
            LogEntry(user_id=user_id, kind=LogKind.TRIP, title="Viagem SP", started_at=datetime.now(UTC))
        )

        report = CommunityReport(
            user_id=user_id,
            category=ReportCategory.SECURITY,
            title="Buraco na rua",
            description="Buraco perigoso",
            status=ReportStatus.SUBMITTED,
            photo_path=photo_name,
        )
        session.add(report)
        await session.flush()
        session.add(ReportConfirmation(report_id=report.id, user_id=user_id))
        session.add(ReportFlag(report_id=report.id, user_id=user_id, reason="spam"))
        # Another user also confirms the report — must be wiped with the report.
        session.add(ReportConfirmation(report_id=report.id, user_id=other_id))
        session.add(UserBlock(blocker_id=user_id, blocked_id=other_id))
        session.add(
            DraftComplaint(user_id=user_id, report_id=report.id, recipient="Prefeitura", body="Texto do rascunho")
        )

        await session.commit()
        return {"report": report.id}


def test_export_includes_every_module(client: TestClient, tmp_path, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "community_media_dir", str(tmp_path))
    force_memory_redis(monkeypatch)

    body = _register(client, "coverage-export@example.com")
    user_id = int(body["user"]["id"])
    headers = _auth(body["access_token"])

    async def _setup() -> int:
        async with SessionLocal() as session:
            other = User(
                full_name="Other",
                email="coverage-other@example.com",
                password_hash="x",
                birth_date=date(1990, 1, 1),
                role=UserRole.USER,
            )
            session.add(other)
            await session.commit()
            await session.refresh(other)
            return other.id

    other_id = asyncio.run(_setup())

    photo = tmp_path / f"{user_id}_photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    asyncio.run(_seed_and_return_ids(user_id, other_id, photo.name))

    resp = client.get(f"{_API}/me/export", headers=headers)
    assert resp.status_code == 200, resp.text
    bundle = resp.json()

    assert bundle["health"]["medications"][0]["name"] == "Dipirona"
    assert bundle["health"]["medications"][0]["schedule_times"] == ["08:00", "20:00"]
    assert len(bundle["health"]["intakes"]) == 1
    assert bundle["finance"]["expenses"][0]["amount_cents"] == 1234
    assert bundle["finance"]["incomes"][0]["source"] == "salary"
    assert bundle["finance"]["bills"][0]["name"] == "Luz"
    assert bundle["finance"]["budgets"][0]["month"] == "2026-02"
    assert bundle["study"]["plans"][0]["subject"] == "Matemática"
    assert bundle["study"]["decks"][0]["cards"][0]["front"] == "to be"
    assert len(bundle["study"]["reviews"]) == 1
    assert bundle["logbook"]["entries"][0]["title"] == "Viagem SP"
    assert bundle["community"]["reports"][0]["title"] == "Buraco na rua"
    assert bundle["community"]["reports"][0]["photo_path"] == photo.name
    assert len(bundle["community"]["confirmations"]) == 1
    assert len(bundle["community"]["flags"]) == 1
    assert len(bundle["community"]["blocks"]) == 1
    assert bundle["community"]["drafts"][0]["body"] == "Texto do rascunho"

    assert "password_hash" not in resp.text
    assert "purchase_token_hash" not in resp.text


def _table_counts(user_id: int, other_id: int, report_id: int) -> dict[str, int]:
    async def _run() -> dict[str, int]:
        async with SessionLocal() as session:
            async def count(model, *criteria) -> int:
                stmt = select(func.count()).select_from(model).where(*criteria)
                return int((await session.execute(stmt)).scalar_one())

            return {
                "medications": await count(Medication, Medication.user_id == user_id),
                "intakes": await count(MedicationIntake, MedicationIntake.user_id == user_id),
                "expenses": await count(Expense, Expense.user_id == user_id),
                "incomes": await count(Income, Income.user_id == user_id),
                "bills": await count(Bill, Bill.user_id == user_id),
                "budgets": await count(Budget, Budget.user_id == user_id),
                "plans": await count(StudyPlan, StudyPlan.user_id == user_id),
                "decks": await count(FlashcardDeck, FlashcardDeck.user_id == user_id),
                "cards": await count(Flashcard, Flashcard.user_id == user_id),
                "reviews": await count(FlashcardReview, FlashcardReview.user_id == user_id),
                "logs": await count(LogEntry, LogEntry.user_id == user_id),
                "reports": await count(CommunityReport, CommunityReport.user_id == user_id),
                "confirmations": await count(ReportConfirmation, ReportConfirmation.user_id == user_id),
                "flags": await count(ReportFlag, ReportFlag.user_id == user_id),
                "blocks": await count(UserBlock, UserBlock.blocker_id == user_id),
                "drafts": await count(DraftComplaint, DraftComplaint.user_id == user_id),
                # cross-user: another user's confirmation on the deleted report
                "other_confirmation_on_report": await count(
                    ReportConfirmation, ReportConfirmation.report_id == report_id
                ),
                "other_user": await count(User, User.id == other_id),
            }

    return asyncio.run(_run())


def test_delete_wipes_every_module_and_photo(client: TestClient, tmp_path, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "community_media_dir", str(tmp_path))
    force_memory_redis(monkeypatch)

    body = _register(client, "coverage-delete@example.com")
    user_id = int(body["user"]["id"])
    headers = _auth(body["access_token"])

    async def _setup_other() -> int:
        async with SessionLocal() as session:
            other = User(
                full_name="Other",
                email="coverage-delete-other@example.com",
                password_hash="x",
                birth_date=date(1990, 1, 1),
                role=UserRole.USER,
            )
            session.add(other)
            await session.commit()
            await session.refresh(other)
            return other.id

    other_id = asyncio.run(_setup_other())

    photo = tmp_path / f"{user_id}_photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    ids = asyncio.run(_seed_and_return_ids(user_id, other_id, photo.name))
    assert photo.exists()

    resp = client.request("DELETE", f"{_API}/me", json={"password": _PASSWORD}, headers=headers)
    assert resp.status_code == 200, resp.text

    counts = _table_counts(user_id, other_id, ids["report"])
    for key in (
        "medications",
        "intakes",
        "expenses",
        "incomes",
        "bills",
        "budgets",
        "plans",
        "decks",
        "cards",
        "reviews",
        "logs",
        "reports",
        "confirmations",
        "flags",
        "blocks",
        "drafts",
        "other_confirmation_on_report",
    ):
        assert counts[key] == 0, f"{key} still has rows: {counts[key]}"
    assert counts["other_user"] == 1, "the other user must survive"

    assert not photo.exists(), "the community photo file must be deleted"


def test_delete_report_removes_photo_file(client: TestClient, tmp_path, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "community_media_dir", str(tmp_path))
    force_memory_redis(monkeypatch)

    body = _register(client, "coverage-photo@example.com")
    user_id = int(body["user"]["id"])
    headers = _auth(body["access_token"])

    photo = tmp_path / f"{user_id}_photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")

    async def _seed_report() -> int:
        async with SessionLocal() as session:
            report = CommunityReport(
                user_id=user_id,
                category=ReportCategory.OTHER,
                title="Com foto",
                description="desc",
                photo_path=photo.name,
            )
            session.add(report)
            await session.commit()
            await session.refresh(report)
            return report.id

    report_id = asyncio.run(_seed_report())
    assert photo.exists()

    resp = client.delete(f"{_API}/community/reports/{report_id}", headers=headers)
    assert resp.status_code in {200, 204}, resp.text
    assert not photo.exists(), "deleting a report must unlink its photo"
