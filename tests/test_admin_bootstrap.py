"""
Tests for the startup operator bootstrap (:mod:`app.db.bootstrap`).

The API creates the operator (admin) account by itself, on every boot, from
the ``ADMIN_BOOTSTRAP_*`` environment variables.  These tests cover the
contract that matters for an operator:

1. the account is created when it is missing (with every NOT NULL column
   filled and the terms/privacy consents of the register flow);
2. the bootstrap is idempotent — a second boot changes nothing;
3. the password of an existing account is never overwritten;
4. an existing account is promoted only when ``ADMIN_BOOTSTRAP_PROMOTE_EXISTING``
   is on (and is never demoted);
5. production refuses a weak credential without crashing the app;
6. two workers booting at the same time create exactly one account;
7. every state change is written to ``admin_audit_log``;
8. the panel login (``/admin/login``) accepts the bootstrapped account.
"""

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Generator

import pytest
from app.core.config import Settings
from app.core.security import pwd_context
from app.db.bootstrap import (
    ACTION_CREATED,
    ACTION_PROMOTED,
    OUTCOME_CREATED,
    OUTCOME_PROMOTED,
    OUTCOME_SKIPPED,
    OUTCOME_UNCHANGED,
    bootstrap_admin_account,
)
from app.db.session import SessionLocal
from app.main import app
from app.models.admin_audit_log import AdminAuditLog
from app.models.consent import ConsentRecord
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

_EMAIL = "ops@example.com"
_PASSWORD = "Str0ngOperator!42"
_OTHER_PASSWORD = "AnotherStrong!77"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def clean_bootstrap_data() -> AsyncGenerator[None, None]:
    """Remove the rows the bootstrap touches so each test starts clean."""
    async with SessionLocal() as session:
        await session.execute(
            delete(ConsentRecord).where(
                ConsentRecord.user_id.in_(select(User.id).where(User.email == _EMAIL))
            )
        )
        await session.execute(delete(User).where(User.email == _EMAIL))
        await session.execute(
            delete(AdminAuditLog).where(AdminAuditLog.action.in_([ACTION_CREATED, ACTION_PROMOTED]))
        )
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(
            delete(ConsentRecord).where(
                ConsentRecord.user_id.in_(select(User.id).where(User.email == _EMAIL))
            )
        )
        await session.execute(delete(User).where(User.email == _EMAIL))
        await session.execute(
            delete(AdminAuditLog).where(AdminAuditLog.action.in_([ACTION_CREATED, ACTION_PROMOTED]))
        )
        await session.commit()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a :class:`TestClient` for the whole app (lifespan included)."""
    with TestClient(app) as c:
        yield c


def _settings(**overrides: object) -> Settings:
    """Build settings for the bootstrap, independent of the local ``.env``."""
    values: dict[str, object] = {
        "admin_bootstrap_email": _EMAIL,
        "admin_bootstrap_password": _PASSWORD,
    }
    values.update(overrides)
    return Settings(**values)


async def _user(email: str = _EMAIL) -> User | None:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalars().first()


async def _count_users(email: str = _EMAIL) -> int:
    async with SessionLocal() as session:
        result = await session.execute(
            select(func.count()).select_from(User).where(User.email == email)
        )
        return int(result.scalar_one())


async def _consent_types(user_id: int) -> list[str]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(ConsentRecord.type).where(ConsentRecord.user_id == user_id)
        )
        return sorted(result.scalars().all())


async def _audit_rows(action: str) -> list[AdminAuditLog]:
    async with SessionLocal() as session:
        result = await session.execute(select(AdminAuditLog).where(AdminAuditLog.action == action))
        return list(result.scalars().all())


async def _create_user(email: str, password: str, role: UserRole = UserRole.USER) -> User:
    """Insert a user the way the register flow would."""
    async with SessionLocal() as session:
        user = User(
            full_name="Usuário Existente",
            email=email,
            password_hash=pwd_context.hash(password),
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match is not None, "csrf token not found in page"
    return match.group(1)


def _panel_login(client: TestClient, email: str, password: str):
    """POST the panel login form with a freshly minted pre-auth CSRF token."""
    page = client.get("/admin/login")
    token = _csrf_from(page.text)
    data = {"email": email, "password": password, "csrf_token": token, "totp_code": ""}
    return client.post("/admin/login", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------
# 1. Creation
# ---------------------------------------------------------------------------


async def test_creates_the_operator_when_it_is_absent(clean_bootstrap_data: None) -> None:
    """A missing account is created as ADMIN, with every NOT NULL column filled."""
    outcome = await bootstrap_admin_account(_settings(admin_bootstrap_name="Operadora"))

    assert outcome == OUTCOME_CREATED
    admin = await _user()
    assert admin is not None
    assert admin.email == _EMAIL
    assert admin.role == UserRole.ADMIN
    assert admin.full_name == "Operadora"
    assert pwd_context.verify(_PASSWORD, admin.password_hash)
    assert admin.birth_date is not None
    assert admin.refresh_token_valid_after is not None


async def test_creates_the_terms_and_privacy_consents(clean_bootstrap_data: None) -> None:
    """The account gets the same consents the register flow records."""
    await bootstrap_admin_account(_settings())

    admin = await _user()
    assert admin is not None
    assert await _consent_types(admin.id) == ["privacy", "terms"]


async def test_the_lifespan_bootstraps_the_account(
    clean_bootstrap_data: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting the API (lifespan) creates the operator account on its own."""
    import app.db.bootstrap as bootstrap_module

    settings = _settings()
    monkeypatch.setattr(bootstrap_module, "get_settings", lambda: settings)

    async with app.router.lifespan_context(app):
        pass

    admin = await _user()
    assert admin is not None
    assert admin.role == UserRole.ADMIN
    assert pwd_context.verify(_PASSWORD, admin.password_hash)


# ---------------------------------------------------------------------------
# 2. Idempotence
# ---------------------------------------------------------------------------


async def test_is_idempotent_on_a_second_boot(clean_bootstrap_data: None) -> None:
    """A second boot creates nothing and writes no second audit entry."""
    settings = _settings()

    assert await bootstrap_admin_account(settings) == OUTCOME_CREATED
    assert await bootstrap_admin_account(settings) == OUTCOME_UNCHANGED

    assert await _count_users() == 1
    assert len(await _audit_rows(ACTION_CREATED)) == 1


# ---------------------------------------------------------------------------
# 3. An existing account is never overwritten
# ---------------------------------------------------------------------------


async def test_does_not_change_the_password_of_an_existing_account(clean_bootstrap_data: None) -> None:
    """The stored hash survives the bootstrap untouched."""
    existing = await _create_user(_EMAIL, _OTHER_PASSWORD)
    original_hash = existing.password_hash

    outcome = await bootstrap_admin_account(_settings())

    assert outcome == OUTCOME_UNCHANGED
    user = await _user()
    assert user is not None
    assert user.password_hash == original_hash
    assert pwd_context.verify(_OTHER_PASSWORD, user.password_hash)
    assert not pwd_context.verify(_PASSWORD, user.password_hash)


async def test_never_demotes_an_existing_admin(clean_bootstrap_data: None) -> None:
    """An account that is already ADMIN keeps its role."""
    await _create_user(_EMAIL, _OTHER_PASSWORD, role=UserRole.ADMIN)

    await bootstrap_admin_account(_settings())

    user = await _user()
    assert user is not None
    assert user.role == UserRole.ADMIN


# ---------------------------------------------------------------------------
# 4. Promotion flag
# ---------------------------------------------------------------------------


async def test_promotes_an_existing_account_when_the_flag_is_on(clean_bootstrap_data: None) -> None:
    """``ADMIN_BOOTSTRAP_PROMOTE_EXISTING`` promotes a non-admin account."""
    existing = await _create_user(_EMAIL, _OTHER_PASSWORD)
    original_hash = existing.password_hash
    original_valid_after = existing.refresh_token_valid_after

    outcome = await bootstrap_admin_account(_settings(admin_bootstrap_promote_existing=True))

    assert outcome == OUTCOME_PROMOTED
    user = await _user()
    assert user is not None
    assert user.role == UserRole.ADMIN
    # The password is still the one the account already had.
    assert user.password_hash == original_hash
    # The promotion invalidates the refresh tokens issued before it.
    assert user.refresh_token_valid_after > original_valid_after
    assert len(await _audit_rows(ACTION_PROMOTED)) == 1


async def test_does_not_promote_when_the_flag_is_off(clean_bootstrap_data: None) -> None:
    """Without the flag an existing account is left exactly as it is."""
    existing = await _create_user(_EMAIL, _OTHER_PASSWORD)
    original_valid_after = existing.refresh_token_valid_after

    outcome = await bootstrap_admin_account(_settings(admin_bootstrap_promote_existing=False))

    assert outcome == OUTCOME_UNCHANGED
    user = await _user()
    assert user is not None
    assert user.role == UserRole.USER
    # Nothing is bumped either: the account is left completely untouched.
    assert user.refresh_token_valid_after == original_valid_after
    assert await _audit_rows(ACTION_PROMOTED) == []


# ---------------------------------------------------------------------------
# 5. Production safety
# ---------------------------------------------------------------------------


async def test_refuses_a_weak_password_in_production(
    clean_bootstrap_data: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A weak credential is refused: no account, no crash, no secret in the log."""
    settings = _settings(environment="production", admin_bootstrap_password="admin123")

    with caplog.at_level(logging.ERROR):
        outcome = await bootstrap_admin_account(settings)

    assert outcome == OUTCOME_SKIPPED
    assert await _count_users() == 0
    assert any("refused" in record.message for record in caplog.records)
    # The password (and its hash) never reach the log.
    assert "admin123" not in caplog.text


async def test_refuses_a_short_password_in_production(clean_bootstrap_data: None) -> None:
    """Fewer than 12 characters is refused in production."""
    settings = _settings(environment="production", admin_bootstrap_password="Short1!")

    outcome = await bootstrap_admin_account(settings)

    assert outcome == OUTCOME_SKIPPED
    assert await _count_users() == 0


async def test_refuses_an_invalid_email(clean_bootstrap_data: None) -> None:
    """A malformed address is refused in every environment."""
    settings = _settings(admin_bootstrap_email="not-an-email")

    outcome = await bootstrap_admin_account(settings)

    assert outcome == OUTCOME_SKIPPED
    assert await _count_users() == 0


@pytest.mark.parametrize(
    "password",
    ["change-me", "admin", "admin123", "password", "12345678", "change-me-in-production", "your-groq-api-key-here"],
)
async def test_refuses_every_denylisted_password_in_production(
    clean_bootstrap_data: None, password: str
) -> None:
    """The deny-list (classic defaults + the ``.env.example`` placeholders) is refused."""
    settings = _settings(environment="production", admin_bootstrap_password=password)

    outcome = await bootstrap_admin_account(settings)

    assert outcome == OUTCOME_SKIPPED
    assert await _count_users() == 0


async def test_warns_once_when_no_operator_can_exist(
    clean_bootstrap_data: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Without bootstrap variables the panel is reported as operator-less."""
    # The warning only fires when the database really has no admin, so the
    # leftovers of other tests are cleared first.
    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.role == UserRole.ADMIN))
        await session.commit()

    # No bootstrap credentials at all: the deprecated aliases are neutralised
    # so the local ``.env`` cannot fill them in.
    settings = Settings(
        admin_bootstrap_email=None,
        admin_bootstrap_password=None,
        admin_default_email=None,
        admin_default_password=None,
        default_admin_password=None,
    )

    with caplog.at_level(logging.WARNING):
        outcome = await bootstrap_admin_account(settings)

    assert outcome == OUTCOME_SKIPPED
    assert any("no operator" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# 5b. The failure path
# ---------------------------------------------------------------------------


class _FakeSelectResult:
    """Result of the lookup query: the e-mail is free."""

    def scalar_one_or_none(self) -> None:
        return None


class _FakeBegin:
    """Async context manager standing in for ``session.begin()``."""

    async def __aenter__(self) -> "_FakeBegin":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FailingInsertSession:
    """Session that answers the lookup and fails on the insert."""

    def get_bind(self) -> None:
        return None

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, statement: object, *args: object, **kwargs: object) -> object:
        if getattr(statement, "is_select", False):
            return _FakeSelectResult()
        params = statement.compile().params  # type: ignore[attr-defined]
        raise RuntimeError(f"insert failed with params {params}")


class _FakeSessionLocal:
    """Stand-in for the session factory that always fails on the insert."""

    async def __aenter__(self) -> _FailingInsertSession:
        return _FailingInsertSession()

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_a_failure_logs_the_reason_but_never_the_secret(
    clean_bootstrap_data: None, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing insert is reported, but neither the password nor its hash leaks."""
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(bootstrap_module, "SessionLocal", lambda: _FakeSessionLocal())

    with caplog.at_level(logging.ERROR):
        outcome = await bootstrap_admin_account(_settings())

    assert outcome == OUTCOME_SKIPPED
    # The reason is logged…
    assert "insert failed" in caplog.text
    # …but the credential and its hash are scrubbed.
    assert _PASSWORD not in caplog.text
    assert "$pbkdf2-sha256$" not in caplog.text
    assert "***" in caplog.text
    # The app keeps serving: no account was created.
    assert await _count_users() == 0


def test_the_scrubber_hides_every_secret() -> None:
    """``_scrub`` removes each secret from a text."""
    from app.db.bootstrap import _scrub

    text = "password=Str0ngOperator!42 hash=$pbkdf2-sha256$abc$def"
    scrubbed = _scrub(text, ["Str0ngOperator!42", "$pbkdf2-sha256$abc$def"])

    assert "Str0ngOperator!42" not in scrubbed
    assert "$pbkdf2-sha256$abc$def" not in scrubbed
    assert "***" in scrubbed


# ---------------------------------------------------------------------------
# 6. Concurrency
# ---------------------------------------------------------------------------


async def test_a_concurrent_double_startup_creates_exactly_one_row(clean_bootstrap_data: None) -> None:
    """Two workers booting together still produce a single account."""
    settings = _settings()

    outcomes = await asyncio.gather(
        bootstrap_admin_account(settings),
        bootstrap_admin_account(settings),
    )

    assert OUTCOME_CREATED in outcomes
    assert await _count_users() == 1
    assert len(await _audit_rows(ACTION_CREATED)) == 1


# ---------------------------------------------------------------------------
# 7. Audit trail
# ---------------------------------------------------------------------------


async def test_writes_one_audit_entry_for_the_creation(clean_bootstrap_data: None) -> None:
    """The creation is audited once, by ``system``, without secrets."""
    await bootstrap_admin_account(_settings())

    rows = await _audit_rows(ACTION_CREATED)
    assert len(rows) == 1
    row = rows[0]
    admin = await _user()
    assert admin is not None
    assert row.admin_email == "system"
    assert row.admin_id is None
    assert row.entity == "user"
    assert row.entity_id == str(admin.id)
    assert _PASSWORD not in str(row.after)
    assert admin.password_hash not in str(row.after)


# ---------------------------------------------------------------------------
# 8. Deprecated aliases
# ---------------------------------------------------------------------------


def _neutralise_canonical_bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty the canonical variables so the alias path is the one under test.

    The local ``.env`` may define ``ADMIN_BOOTSTRAP_*``; an empty environment
    variable wins over the dotenv file, so the deprecated names are the only
    source left for the alias resolver.
    """
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAIL", "")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "")


def test_the_deprecated_aliases_still_feed_the_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADMIN_DEFAULT_*`` keeps working as an alias of ``ADMIN_BOOTSTRAP_*``."""
    _neutralise_canonical_bootstrap_env(monkeypatch)
    monkeypatch.setenv("ADMIN_DEFAULT_EMAIL", "legacy@example.com")
    monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "LegacyStrong!42")

    settings = Settings()

    assert settings.admin_bootstrap_email == "legacy@example.com"
    assert settings.admin_bootstrap_password == "LegacyStrong!42"
    assert "ADMIN_DEFAULT_EMAIL" in settings.admin_bootstrap_deprecated_env
    assert "ADMIN_DEFAULT_PASSWORD" in settings.admin_bootstrap_deprecated_env


def test_the_deprecated_aliases_log_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Using the old names logs exactly one warning — without the password."""
    import app.core.config as config_module

    _neutralise_canonical_bootstrap_env(monkeypatch)
    monkeypatch.setenv("ADMIN_DEFAULT_EMAIL", "legacy@example.com")
    monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "LegacyStrong!42")
    monkeypatch.setattr(config_module, "_settings", None)

    with caplog.at_level(logging.WARNING):
        config_module.get_settings()

    warnings = [record for record in caplog.records if "Deprecated admin bootstrap" in record.message]
    assert len(warnings) == 1
    assert "LegacyStrong!42" not in caplog.text


def test_the_canonical_variables_win_over_the_deprecated_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both spellings set, the canonical name is used and nothing warns."""
    import app.core.config as config_module

    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAIL", "canonical@example.com")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "CanonicalStrong!42")
    monkeypatch.setenv("ADMIN_DEFAULT_EMAIL", "legacy@example.com")
    monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "LegacyStrong!42")
    monkeypatch.setattr(config_module, "_settings", None)

    settings = config_module.get_settings()

    assert settings.admin_bootstrap_email == "canonical@example.com"
    assert settings.admin_bootstrap_password == "CanonicalStrong!42"
    assert settings.admin_bootstrap_deprecated_env == []


# ---------------------------------------------------------------------------
# 9. Panel login
# ---------------------------------------------------------------------------


async def test_the_panel_login_accepts_the_bootstrapped_account(
    clean_bootstrap_data: None, client: TestClient
) -> None:
    """The bootstrapped account logs in at ``/admin/login`` like any ADMIN."""
    await bootstrap_admin_account(_settings())

    response = _panel_login(client, _EMAIL, _PASSWORD)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin")
    assert "novaiax_admin_panel" in response.cookies


async def test_the_panel_login_rejects_a_wrong_password(
    clean_bootstrap_data: None, client: TestClient
) -> None:
    """A wrong password is refused with the generic panel error."""
    await bootstrap_admin_account(_settings())

    response = _panel_login(client, _EMAIL, "WrongPassword!42")

    assert response.status_code == 401
    assert "novaiax_admin_panel" not in response.cookies


async def test_the_bootstrap_admin_must_enroll_totp_when_it_is_required(
    clean_bootstrap_data: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``ADMIN_REQUIRE_TOTP`` the account is sent to the enrolment page."""
    import app.core.config as config_module

    await bootstrap_admin_account(_settings())

    settings = config_module.get_settings()
    monkeypatch.setattr(
        config_module,
        "_settings",
        settings.model_copy(update={"admin_require_totp": True}),
    )

    response = _panel_login(client, _EMAIL, _PASSWORD)
    assert response.status_code == 303

    dashboard = client.get("/admin/", follow_redirects=False)
    assert dashboard.status_code in (302, 303, 307)
    assert dashboard.headers["location"].startswith("/admin/security/totp")
