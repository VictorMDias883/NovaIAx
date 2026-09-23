"""
Tests for the startup ADMIN bootstrap (:mod:`app.db.bootstrap`).

Verifies that on application startup:

1. A default ADMIN account is created when none exists.
2. The account uses ``ADMIN_DEFAULT_EMAIL`` (default ``admin@admin.com``).
3. A randomly generated password (when ``ADMIN_DEFAULT_PASSWORD`` is unset)
   is hashed like a regular user password, and the plain text is logged
   exactly once so the operator can retrieve it.
4. The bootstrap is idempotent — no duplicate admin is created on restart
   once an admin already exists.
"""

import logging

import pytest
from app.core.config import Settings
from app.core.security import pwd_context
from app.db.bootstrap import ensure_default_admin
from app.db.session import SessionLocal
from app.models.user import User, UserRole
from app.repositories.user_repository import UserRepository
from sqlalchemy import delete, func, select


@pytest.fixture
async def clean_users() -> None:
    """Remove every user so each test starts from an empty ``users`` table."""
    async with SessionLocal() as session:
        await session.execute(delete(User))
        await session.commit()
    yield


async def _all_users() -> list[User]:
    async with SessionLocal() as session:
        result = await session.execute(select(User))
        return list(result.scalars().all())


def _find_generated_in(caplog: pytest.LogCaptureFixture) -> str | None:
    """Return the logged password from the ``generated_password`` extra field, if any."""
    for record in caplog.records:
        generated = record.__dict__.get("generated_password")
        if generated:
            return str(generated)
    return None


@pytest.mark.asyncio
async def test_creates_default_admin_when_none_exists(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clean_users: None
) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(admin_default_email="admin@admin.com", admin_default_password=None),
    )

    caplog.set_level(logging.INFO)

    await ensure_default_admin()

    users = await _all_users()
    assert len(users) == 1
    admin = users[0]
    assert admin.email == "admin@admin.com"
    assert admin.role == UserRole.ADMIN

    generated = _find_generated_in(caplog)
    assert generated is not None
    assert admin.password_hash != generated
    assert pwd_context.verify(generated, admin.password_hash)


@pytest.mark.asyncio
async def test_uses_configured_email_and_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clean_users: None
) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(
            admin_default_email="ops@example.com",
            admin_default_password="ConfiguredStrongPass!42",
        ),
    )

    await ensure_default_admin()

    users = await _all_users()
    assert len(users) == 1
    admin = users[0]
    assert admin.email == "ops@example.com"
    assert admin.role == UserRole.ADMIN
    assert pwd_context.verify("ConfiguredStrongPass!42", admin.password_hash)

    assert _find_generated_in(caplog) is None


@pytest.mark.asyncio
async def test_is_idempotent_when_admin_already_exists(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clean_users: None
) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(admin_default_email="admin@admin.com", admin_default_password=None),
    )

    caplog.set_level(logging.INFO)

    await ensure_default_admin()
    await ensure_default_admin()

    async def count_admins() -> int:
        async with SessionLocal() as session:
            result = await session.execute(select(func.count()).select_from(User).where(User.role == UserRole.ADMIN))
            return int(result.scalar_one())

    assert await count_admins() == 1

    generated = _find_generated_in(caplog)
    assert generated is not None

    generated_records = [r for r in caplog.records if r.__dict__.get("generated_password")]
    assert len(generated_records) == 1
    assert str(generated_records[0].__dict__["generated_password"]) == generated


@pytest.mark.asyncio
async def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADMIN_DEFAULT_*`` fall back to ``admin@admin.com`` and a generated password."""
    monkeypatch.delenv("ADMIN_DEFAULT_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_DEFAULT_PASSWORD", raising=False)
    monkeypatch.delenv("DEFAULT_ADMIN_PASSWORD", raising=False)

    # ``_env_file=None`` keeps the assertion independent of a local ``.env``
    # (which may legitimately pin an ``ADMIN_DEFAULT_EMAIL``).
    settings = Settings(_env_file=None)
    assert settings.admin_default_email == "admin@admin.com"
    assert settings.admin_default_password is None


async def test_accepts_legacy_default_admin_password_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy ``DEFAULT_ADMIN_PASSWORD`` env var still feeds the bootstrap field."""
    monkeypatch.delenv("ADMIN_DEFAULT_PASSWORD", raising=False)
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "LegacyPass!42")

    settings = Settings(_env_file=None)
    assert settings.admin_default_password == "LegacyPass!42"


@pytest.mark.asyncio
async def test_resets_admin_password_to_configured_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clean_users: None
) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(admin_default_email="admin@example.com", admin_default_password=None),
    )
    await ensure_default_admin()
    await _all_users()
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == "admin@example.com"))
        before = result.scalar_one()
        assert pwd_context.verify("garbage-not-the-password", before.password_hash) is False
        old_hash = before.password_hash

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(
            admin_default_email="admin@example.com",
            admin_default_password="O5hZO4NgL8qE9UMXtdTO",
        ),
    )

    caplog.set_level(logging.WARNING)
    await ensure_default_admin()

    users = await _all_users()
    assert len(users) == 1
    admin = users[0]
    assert admin.email == "admin@example.com"
    assert admin.role == UserRole.ADMIN
    assert pwd_context.verify("O5hZO4NgL8qE9UMXtdTO", admin.password_hash)
    assert admin.password_hash != old_hash

    reset_warnings = [r for r in caplog.records if r.getMessage() and "reset" in str(r.getMessage()).lower()]
    assert len(reset_warnings) == 1


@pytest.mark.asyncio
async def test_does_not_reset_when_password_matches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, clean_users: None
) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(
            admin_default_email="admin@example.com",
            admin_default_password="StablePass!42",
        ),
    )
    await ensure_default_admin()

    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == "admin@example.com"))
        old_hash = result.scalar_one().password_hash

    caplog.set_level(logging.WARNING)
    await ensure_default_admin()

    users = await _all_users()
    assert len(users) == 1
    assert users[0].password_hash == old_hash
    assert pwd_context.verify("StablePass!42", users[0].password_hash)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_does_not_touch_other_admins(monkeypatch: pytest.MonkeyPatch, clean_users: None) -> None:
    import app.db.bootstrap as bootstrap_module

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(admin_default_email="ops@example.com", admin_default_password=None),
    )
    await ensure_default_admin()

    async with SessionLocal() as session:
        repo = UserRepository(session)
        async with session.begin():
            await repo.create(
                full_name="Other Admin",
                email="other-admin@example.com",
                password_hash=pwd_context.hash("KeepThisHash!42"),
                role=UserRole.ADMIN,
                commit=False,
            )
        async with session.begin():
            result = await session.execute(select(User).where(User.email == "other-admin@example.com"))
            other_hash = result.scalar_one().password_hash

    monkeypatch.setattr(
        bootstrap_module,
        "get_settings",
        lambda: Settings(
            admin_default_email="ops@example.com",
            admin_default_password="SyncedPass!42",
        ),
    )
    await ensure_default_admin()

    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == "ops@example.com"))
        ops = result.scalar_one()
        result = await session.execute(select(User).where(User.email == "other-admin@example.com"))
        other_after = result.scalar_one()

    assert pwd_context.verify("SyncedPass!42", ops.password_hash)
    assert other_after.password_hash == other_hash
    assert pwd_context.verify("KeepThisHash!42", other_after.password_hash)

    async with SessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(User).where(User.role == UserRole.ADMIN))
        assert int(result.scalar_one()) == 2
