"""
Integration tests for the privacy & compliance suite.

Covers the age gate, the consent log, data export, password-protected account
deletion, the public compliance pages, failed-login lockout and conversation
retention TTLs:

1. **Age gate** — ``birth_date`` is required at registration and anyone under
   ``minimum_account_age_years`` is refused with a 422.
2. **Consent log** — ``GET/PUT/DELETE /api/v1/me/consents`` read, grant and
   withdraw; registration auto-records ``terms`` + ``privacy``.
3. **Data export** — ``GET /api/v1/me/export`` returns a portable bundle
   (never ``password_hash``/``purchase_token_hash``) and is rate-limited per
   user.
4. **Account deletion** — ``DELETE /api/v1/me`` requires the account
   password, wipes owned rows, revokes access and surfaces the Play
   cancellation URL when a subscription exists; the public
   ``/account-deletion`` form supports both the immediate (email+password)
   and queued (email-only) flows.
5. **Failed-login lockout** — repeated wrong passwords for one email
   eventually return a 429.
6. **Retention TTL** — conversational history keys carry the configured
   rolling expiry.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from app.cache.conversation_cache import ConversationCache
from app.db.session import SessionLocal
from app.main import app
from app.models.consent import ConsentRecord
from app.models.deletion_request import DeletionRequest
from app.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.models.user import User
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from tests.helpers import force_memory_redis, make_conversation_cache

_PASSWORD = "Password123"
_API = "/api/v1"

_REGISTER = {
    "full_name": "Privacy User",
    "password": _PASSWORD,
    "birth_date": "1990-01-01",
}


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
            await session.execute(delete(DeletionRequest).where(DeletionRequest.email.in_(emails)))
            await session.commit()


def _register(client: TestClient, email: str) -> dict:
    """Register a user (adult by default) and return the response body."""
    resp = client.post(f"{_API}/auth/register", json={**_REGISTER, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient, email: str, password: str = _PASSWORD) -> int:
    return client.post(f"{_API}/auth/login", json={"email": email, "password": password}).status_code


async def _count_consents(user_id: int) -> int:
    async with SessionLocal() as session:
        stmt = select(ConsentRecord).where(ConsentRecord.user_id == user_id)
        return len((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Age gate
# ---------------------------------------------------------------------------


def test_register_refuses_a_minor(client: TestClient) -> None:
    resp = client.post(
        f"{_API}/auth/register",
        json={**_REGISTER, "email": "minor@example.com", "birth_date": "2015-01-01"},
    )
    assert resp.status_code == 422, resp.text


def test_register_refuses_one_day_before_eighteenth_birthday(client: TestClient) -> None:
    today = date.today()
    just_under = date(today.year - 17, today.month, today.day)
    resp = client.post(
        f"{_API}/auth/register",
        json={**_REGISTER, "email": "edge-under@example.com", "birth_date": just_under.isoformat()},
    )
    assert resp.status_code == 422, resp.text


def test_register_adult_records_default_consents(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("consent-defaults@example.com")
    body = _register(client, "consent-defaults@example.com")

    consents = client.get(f"{_API}/me/consents", headers=_auth(body["access_token"]))
    assert consents.status_code == 200, consents.text
    types = {item["type"]: item["version"] for item in consents.json()["consents"]}
    assert set(types) == {"terms", "privacy"}
    assert types["terms"] and types["privacy"]


def test_register_requires_birth_date(client: TestClient) -> None:
    payload = {key: value for key, value in _REGISTER.items() if key != "birth_date"}
    resp = client.post(f"{_API}/auth/register", json={**payload, "email": "no-dob@example.com"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Consent log
# ---------------------------------------------------------------------------


def test_consent_grant_and_withdraw(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("consents@example.com")
    body = _register(client, "consents@example.com")
    headers = _auth(body["access_token"])

    granted = client.put(f"{_API}/me/consents", json={"type": "microphone"}, headers=headers)
    assert granted.status_code == 200, granted.text
    assert granted.json()["type"] == "microphone"

    types = {item["type"] for item in client.get(f"{_API}/me/consents", headers=headers).json()["consents"]}
    assert "microphone" in types

    withdrawn = client.delete(f"{_API}/me/consents/microphone", headers=headers)
    assert withdrawn.status_code == 200, withdrawn.text

    types = {item["type"] for item in client.get(f"{_API}/me/consents", headers=headers).json()["consents"]}
    assert "microphone" not in types


def test_consent_unknown_type_is_rejected(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("consent-bad@example.com")
    body = _register(client, "consent-bad@example.com")
    headers = _auth(body["access_token"])

    # Unknown types are rejected at the schema (422) and, on withdrawal where
    # the path is a free string, as a 404 (nothing was ever granted).
    assert client.put(f"{_API}/me/consents", json={"type": "telemetry"}, headers=headers).status_code == 422
    assert client.delete(f"{_API}/me/consents/telemetry", headers=headers).status_code == 404
    assert client.delete(f"{_API}/me/consents/contacts", headers=headers).status_code == 404


def test_consent_reaccept_updates_in_place(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("consent-version@example.com")
    body = _register(client, "consent-version@example.com")
    headers = _auth(body["access_token"])
    initial_history = client.get(f"{_API}/me/export", headers=headers).json()["consent_history"]

    first = client.get(f"{_API}/me/consents", headers=headers).json()["consents"]
    original_accepted_at = next(item for item in first if item["type"] == "terms")["accepted_at"]

    re_accepted = client.put(f"{_API}/me/consents", json={"type": "terms", "version": "2"}, headers=headers)
    assert re_accepted.status_code == 200, re_accepted.text
    assert re_accepted.json()["version"] == "2"

    second = client.get(f"{_API}/me/consents", headers=headers).json()["consents"]
    terms_entries = [item for item in second if item["type"] == "terms"]
    assert len(terms_entries) == 1
    assert terms_entries[0]["accepted_at"] >= original_accepted_at

    assert client.delete(f"{_API}/me/consents/terms", headers=headers).status_code == 200
    export = client.get(f"{_API}/me/export", headers=headers).json()
    terms_history = [item for item in export["consent_history"] if item["type"] == "terms"]
    initial_terms_count = sum(item["type"] == "terms" for item in initial_history)
    added_terms = terms_history[initial_terms_count:]
    assert [item["action"] for item in added_terms] == ["reaffirmed", "withdrawn"]
    assert [item["version"] for item in added_terms] == ["2", "2"]


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------


def test_export_returns_a_portable_bundle(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("export@example.com")
    body = _register(client, "export@example.com")

    resp = client.get(f"{_API}/me/export", headers=_auth(body["access_token"]))
    assert resp.status_code == 200, resp.text
    bundle = resp.json()

    assert bundle["user"]["email"] == "export@example.com"
    assert bundle["user"]["birth_date"] == "1990-01-01"
    assert set(bundle["user"]) == {"id", "full_name", "email", "role", "birth_date", "created_at"}
    assert {item["type"] for item in bundle["consents"]} == {"terms", "privacy"}
    assert "password_hash" not in resp.text
    assert "purchase_token_hash" not in resp.text
    for key in ("objectives", "user_agent", "memories", "agent_actions", "ai_feedback", "subscription"):
        assert key in bundle
    for key in ("tasks", "reminders", "notes", "checklists", "habits", "focus_sessions"):
        assert key in bundle["productivity"]
    assert isinstance(bundle["third_party_processors"], list)


def test_export_seeds_productivity_data(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("export-content@example.com")
    body = _register(client, "export-content@example.com")
    headers = _auth(body["access_token"])

    created = client.post(f"{_API}/me/tasks", json={"title": "Comprar pão"}, headers=headers)
    assert created.status_code in {200, 201}, created.text

    bundle = client.get(f"{_API}/me/export", headers=headers).json()
    assert any(task["title"] == "Comprar pão" for task in bundle["productivity"]["tasks"])


def test_export_includes_cached_conversation(client: TestClient, created_emails: list[str], monkeypatch) -> None:
    shared = force_memory_redis(monkeypatch)
    created_emails.append("export-conversation@example.com")
    body = _register(client, "export-conversation@example.com")
    user_id = int(body["user"]["id"])
    asyncio.run(
        ConversationCache(shared).save(
            "general", user_id, [{"role": "user", "content": "Minha anotação pessoal"}]
        )
    )

    response = client.get(f"{_API}/me/export", headers=_auth(body["access_token"]))
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["conversations"]["general"] == [
        {"role": "user", "content": "Minha anotação pessoal"}
    ]
    assert "chat_messages" in bundle
    assert "promo_redemptions" in bundle


def test_export_is_rate_limited_per_user(
    client: TestClient, created_emails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    force_memory_redis(monkeypatch)
    created_emails.append("export-limit@example.com")
    monkeypatch.setattr(get_settings(), "data_export_max_per_hour", 1)
    headers = _auth(_register(client, "export-limit@example.com")["access_token"])

    assert client.get(f"{_API}/me/export", headers=headers).status_code == 200
    assert client.get(f"{_API}/me/export", headers=headers).status_code == 429
    assert client.get(f"{_API}/me/export", headers=headers).status_code == 429


def test_export_blocks_api_key_identities(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "master_api_key", "test-master-key")
    # A valid master key authenticates, but has no account to export.
    resp = client.get(f"{_API}/me/export", headers={"X-API-Key": "test-master-key"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Account deletion (API)
# ---------------------------------------------------------------------------


def test_delete_requires_the_account_password(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("del-wrong@example.com")
    body = _register(client, "del-wrong@example.com")
    headers = _auth(body["access_token"])

    resp = client.request("DELETE", f"{_API}/me", json={"password": "NotThePassword"}, headers=headers)
    assert resp.status_code == 400, resp.text
    assert _login(client, "del-wrong@example.com") == 200


def test_delete_wipes_the_account_and_revokes_access(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("del-ok@example.com")
    body = _register(client, "del-ok@example.com")
    headers = _auth(body["access_token"])
    user_id = int(body["user"]["id"])

    resp = client.request("DELETE", f"{_API}/me", json={"password": _PASSWORD}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]
    assert resp.json()["subscription_management_url"] is None

    assert _login(client, "del-ok@example.com") == 401
    assert asyncio.run(_count_consents(user_id)) == 0


def test_delete_clears_conversation_cache(
    client: TestClient, created_emails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = force_memory_redis(monkeypatch)
    created_emails.append("del-cache@example.com")
    body = _register(client, "del-cache@example.com")
    headers = _auth(body["access_token"])
    user_id = int(body["user"]["id"])

    cache = ConversationCache(shared)

    async def _seed() -> None:
        await cache.save("general", user_id, [{"role": "user", "content": "oi"}])
        assert len(await cache.get_messages("general", user_id)) == 1

    asyncio.run(_seed())

    resp = client.request("DELETE", f"{_API}/me", json={"password": _PASSWORD}, headers=headers)
    assert resp.status_code == 200, resp.text

    assert asyncio.run(shared.keys(f"conversation:*:{user_id}")) == []


def test_delete_returns_play_url_when_subscribed(
    client: TestClient, created_emails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:

    created_emails.append("del-sub@example.com")
    body = _register(client, "del-sub@example.com")
    headers = _auth(body["access_token"])
    user_id = int(body["user"]["id"])

    async def _seed_subscription() -> None:
        async with SessionLocal() as session:
            session.add(
                Subscription(
                    user_id=user_id,
                    plan=SubscriptionPlan.PRO,
                    status=SubscriptionStatus.ACTIVE,
                    product_id="novaiax_pro_monthly",
                    purchase_token_hash="a" * 64,
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                )
            )
            await session.commit()

    asyncio.run(_seed_subscription())

    resp = client.request("DELETE", f"{_API}/me", json={"password": _PASSWORD}, headers=headers)
    assert resp.status_code == 200, resp.text
    url = resp.json()["subscription_management_url"]
    assert url is not None
    assert "play.google.com/store/account/subscriptions" in url
    assert "novaiax_pro_monthly" in url

    async def _assert_sub_gone() -> None:
        async with SessionLocal() as session:
            remaining = (
                await session.execute(select(Subscription).where(Subscription.user_id == user_id))
            ).scalars().all()
            assert remaining == []

    asyncio.run(_assert_sub_gone())


# ---------------------------------------------------------------------------
# Public compliance pages
# ---------------------------------------------------------------------------


def test_privacy_page_renders_processor_list(client: TestClient) -> None:
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "Política de Privacidade" in resp.text
    assert "Terceiros que processam seus dados" in resp.text


def test_terms_page_renders(client: TestClient) -> None:
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "Termos de Uso" in resp.text


def test_account_deletion_page_renders_form(client: TestClient) -> None:
    resp = client.get("/account-deletion")
    assert resp.status_code == 200
    assert "Exclusão de conta" in resp.text


def test_public_delete_form_queues_request_without_password(
    client: TestClient, created_emails: list[str]
) -> None:
    created_emails.append("page-queue@example.com")
    client.get("/account-deletion")  # mint the CSRF cookie
    csrf = client.cookies.get("novaiax_public_csrf")
    assert csrf

    resp = client.post(
        "/account-deletion/request",
        data={"email": "page-queue@example.com", "password": "", "csrf_token": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert "Recebemos sua solicitação" in resp.text

    async def _check() -> None:
        async with SessionLocal() as session:
            pending = (
                await session.execute(
                    select(DeletionRequest).where(DeletionRequest.email == "page-queue@example.com")
                )
            ).scalars().all()
            assert len(pending) == 1
            assert pending[0].status == "pending"

    asyncio.run(_check())


def test_public_delete_form_rejects_missing_csrf_cookie(client: TestClient) -> None:
    response = client.post(
        "/account-deletion/request",
        data={"email": "victim@example.com", "password": "", "csrf_token": "attacker-token"},
    )
    assert response.status_code == 403


def test_public_delete_form_deletes_with_password(
    client: TestClient, created_emails: list[str]
) -> None:
    created_emails.append("page-delete@example.com")
    _register(client, "page-delete@example.com")
    client.get("/account-deletion")
    csrf = client.cookies.get("novaiax_public_csrf")
    assert csrf

    resp = client.post(
        "/account-deletion/request",
        data={"email": "page-delete@example.com", "password": _PASSWORD, "csrf_token": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert "removidos permanentemente" in resp.text
    assert _login(client, "page-delete@example.com") == 401


# ---------------------------------------------------------------------------
# Failed-login lockout
# ---------------------------------------------------------------------------


def test_failed_logins_lock_the_address(client: TestClient, created_emails: list[str]) -> None:
    created_emails.append("lockout@example.com")
    _register(client, "lockout@example.com")

    for _ in range(5):
        assert _login(client, "lockout@example.com", "WrongPassword1") == 401

    assert _login(client, "lockout@example.com", _PASSWORD) == 429


def test_successful_login_resets_the_lockout(
    client: TestClient, created_emails: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings

    created_emails.append("lockout-reset@example.com")
    monkeypatch.setattr(get_settings(), "login_failed_attempts_threshold", 3)
    _register(client, "lockout-reset@example.com")

    # 2 failures + 1 success clears the counter.
    assert _login(client, "lockout-reset@example.com", "WrongPassword1") == 401
    assert _login(client, "lockout-reset@example.com", "WrongPassword1") == 401
    assert _login(client, "lockout-reset@example.com", _PASSWORD) == 200
    # With the counter cleared, two more failures still do not lock.
    assert _login(client, "lockout-reset@example.com", "WrongPassword1") == 401
    assert _login(client, "lockout-reset@example.com", "WrongPassword1") == 401
    assert _login(client, "lockout-reset@example.com", _PASSWORD) == 200


# ---------------------------------------------------------------------------
# Conversation retention TTL
# ---------------------------------------------------------------------------


def test_conversation_keys_carry_a_rolling_ttl() -> None:
    cache = make_conversation_cache()

    async def _exercise() -> None:
        await cache.save("general", 1, [{"role": "user", "content": "oi"}])
        ttl = await cache.client.ttl("conversation:general:1")
        assert ttl > 0, "conversation history must expire after the retention window"

        # A later save refreshes the deadline (rolling window).
        await cache.save("general", 1, [{"role": "user", "content": "tudo bem?"}])
        refreshed = await cache.client.ttl("conversation:general:1")
        assert refreshed >= ttl

    asyncio.run(_exercise())
