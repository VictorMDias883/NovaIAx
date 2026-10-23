"""
Tests for the Phase 1/3/4 consolidation work.

Covers the pieces that are easy to regress and expensive to notice:

* the stable ``code`` now present on **every** error envelope;
* the Google Play RTDN push path being reachable without a JWT;
* AI routes threading the caller's plan tier into the quota instead of
  silently charging everyone the free ceiling;
* the small coercion/consolidation helpers.
"""

from __future__ import annotations

import asyncio

import pytest
from app.clients.ai_router import AIRouter
from app.clients.ai_types import AIResponse, PlanTier
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.core.config import Settings
from app.db.session import SessionLocal
from app.exceptions.api_errors import APIError, APIErrorCode, code_for_status
from app.main import app
from app.middlewares.auth_middleware import _is_public_path
from app.models.user import User
from app.services.objective_assistant_service import ObjectiveAssistantService
from fastapi.testclient import TestClient
from sqlalchemy import delete

from tests.helpers import make_conversation_cache

_API = "/api/v1"
_PASSWORD = "Password123"


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


def _register_and_login(client: TestClient, created_emails: list[str], email: str) -> str:
    created_emails.append(email)
    client.post(
        f"{_API}/auth/register",
        json={"full_name": "Envelope User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    login = client.post(f"{_API}/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------


def test_code_for_status_is_total() -> None:
    assert code_for_status(401) == "authentication_required"
    assert code_for_status(404) == "not_found"
    assert code_for_status(409) == "conflict"
    assert code_for_status(422) == "validation_error"
    # A status outside the known set still yields a code, never ``None``.
    assert code_for_status(418) == "bad_request"


def test_apierror_keeps_its_explicit_code() -> None:
    exc = APIError(401, "Credenciais inválidas.", code=APIErrorCode.INVALID_CREDENTIALS)
    assert exc.code == "invalid_credentials"
    assert exc.status_code == 401


def test_validation_error_carries_a_code_without_logging_input(client: TestClient, caplog) -> None:
    secret_value = "private-user-value-912847"
    response = client.post(
        f"{_API}/auth/register",
        json={"full_name": "Name", "email": "valid@example.com", "password": secret_value, "birth_date": "1990-01-01"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Requisição inválida.", "code": "validation_error"}
    assert secret_value not in caplog.text


def test_missing_credentials_error_carries_a_code(client: TestClient) -> None:
    response = client.get(f"{_API}/users/me")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "authentication_required"
    assert "detail" in body


def test_not_found_error_carries_a_code(client: TestClient, created_emails: list[str]) -> None:
    token = _register_and_login(client, created_emails, "envelope-404@example.com")
    response = client.get(
        f"{_API}/objectives/999999/roadmap/days",
        headers={"Authorization": f"Bearer {token}"},
    )
    # The route owner-checks the objective and answers 404; the envelope must
    # carry the stable code so the app does not parse the message.
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# RTDN is a public path (Google calls it, it cannot present a JWT)
# ---------------------------------------------------------------------------


def test_rtdn_path_bypasses_jwt_gate() -> None:
    assert _is_public_path("/api/v1/billing/google/rtdn")
    # The user-facing verify endpoint must stay protected.
    assert not _is_public_path("/api/v1/billing/google/verify")


# ---------------------------------------------------------------------------
# Plan tier threading
# ---------------------------------------------------------------------------


class _CapturingRouter(AIRouter):
    """An ``AIRouter`` whose ``complete`` records the request instead of routing."""

    def __init__(self) -> None:
        super().__init__(
            providers=[],
            quota_tracker=object(),  # type: ignore[arg-type]
            settings=Settings(redis_url="redis://127.0.0.1:1/0"),
        )
        self.requests: list[object] = []

    async def complete(self, request):  # type: ignore[override]
        self.requests.append(request)
        return AIResponse(
            text='{"titulo": "Plano", "descricao": "Teste", "prazo": "2030-01-01"}',
            provider="fake",
            model="fake",
        )


class _StubObjectiveService:
    """Records the ``plan_tier`` it is asked to register with."""

    def __init__(self) -> None:
        self.plan_tiers: list[object] = []

    async def register(self, command, user_id: int, *, plan_tier=None) -> dict:
        self.plan_tiers.append(plan_tier)
        return {"id": 1, "title": command.title, "user_id": user_id}


def test_objective_assistant_threads_plan_tier_into_ai_request() -> None:
    router = _CapturingRouter()
    objective_service = _StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=router,
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    asyncio.run(
        service.create_completion(
            ObjectiveAssistantCommand(user_message="quero uma meta"),
            user_id=7,
            plan_tier=PlanTier.PRO,
        )
    )

    assert router.requests, "the AI request was never issued"
    assert router.requests[0].plan_tier == PlanTier.PRO  # type: ignore[attr-defined]
    # The same tier is handed to the objective registration downstream.
    assert objective_service.plan_tiers == [PlanTier.PRO]


def test_objective_assistant_defaults_to_free() -> None:
    router = _CapturingRouter()
    objective_service = _StubObjectiveService()
    service = ObjectiveAssistantService(
        session=None,
        ai_client=router,
        objective_service=objective_service,
        conversation_cache=make_conversation_cache(),
    )

    asyncio.run(
        service.create_completion(
            ObjectiveAssistantCommand(user_message="quero uma meta"),
            user_id=7,
        )
    )

    assert router.requests[0].plan_tier == PlanTier.FREE  # type: ignore[attr-defined]
    assert objective_service.plan_tiers == [PlanTier.FREE]


# ---------------------------------------------------------------------------
# Coercion helper
# ---------------------------------------------------------------------------


def test_coerce_int_rejects_booleans_and_junk() -> None:
    from app.utils.coercion import coerce_int

    assert coerce_int("12") == 12
    assert coerce_int(3.0) == 3
    assert coerce_int(3.5) is None
    assert coerce_int(True) is None
    assert coerce_int(None) is None
    assert coerce_int("abc") is None


def test_current_user_id_raises_on_missing_id() -> None:
    from app.utils.request import current_user_id

    assert current_user_id({"id": 5}) == 5
    with pytest.raises(ValueError):
        current_user_id({})
