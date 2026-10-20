"""Tests for the follow-up security hardening.

Covers:

1. **Failed-login lockout is keyed on (email, IP)** — an attacker who knows
   only the victim's email cannot lock the real owner out from another
   network, and a successful login clears the pair counter.
2. **No first-registrant auto-admin in production** — the bootstrap ADMIN
   shortcut is disabled whenever ``ENVIRONMENT=production`` so a public
   deployment never hands the ADMIN role to whoever signs up first.
"""

import asyncio
from datetime import date

from app.cache.login_rate_limiter import KEY_PREFIX, LoginRateLimiter
from app.cache.redis_client import RedisClient
from app.commands.register_user_command import RegisterUserCommand
from app.core.config import Settings
from app.db.session import Base
from app.models.user import UserRole
from app.services.auth_service import AuthService
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_MEMORY_REDIS = "redis://127.0.0.1:1/0"


# ---------------------------------------------------------------------------
# 1. Failed-login lockout keyed on (email, IP)
# ---------------------------------------------------------------------------


def test_login_lockout_is_scoped_to_email_and_ip() -> None:
    RedisClient.reset_all_memory_stores()
    settings = Settings(
        redis_url=_MEMORY_REDIS,
        login_failed_attempts_threshold=3,
        login_failed_attempts_window_seconds=900,
    )
    limiter = LoginRateLimiter(RedisClient(settings), settings)

    async def scenario() -> None:
        for _ in range(3):
            await limiter.on_failure("Victim@Example.com", "1.1.1.1")

        # The attacker's (email, IP) pair is locked ...
        assert await limiter.is_locked("victim@example.com", "1.1.1.1") is True
        # ... but the same email from a different IP is untouched, so the
        # real owner is never denied service.
        assert await limiter.is_locked("victim@example.com", "2.2.2.2") is False
        # A different email from the attacker's IP is also unaffected.
        assert await limiter.is_locked("other@example.com", "1.1.1.1") is False

        # A successful login clears only its own pair.
        await limiter.on_success("victim@example.com", "1.1.1.1")
        assert await limiter.is_locked("victim@example.com", "1.1.1.1") is False

    asyncio.run(scenario())


def test_lockout_key_includes_the_ip() -> None:
    """The counter key must scope to the IP, never collapse to email alone."""
    assert LoginRateLimiter._key("a@b.com", "9.9.9.9") == f"{KEY_PREFIX}a@b.com:9.9.9.9"
    # A missing IP is normalised to a stable sentinel rather than dropped.
    assert LoginRateLimiter._key("a@b.com", None) == f"{KEY_PREFIX}a@b.com:unknown"


def test_limiter_can_be_disabled_with_a_zero_threshold() -> None:
    settings = Settings(redis_url=_MEMORY_REDIS, login_failed_attempts_threshold=0)
    limiter = LoginRateLimiter(RedisClient(settings), settings)

    async def scenario() -> None:
        for _ in range(10):
            await limiter.on_failure("nobody@example.com", "1.1.1.1")
        assert await limiter.is_locked("nobody@example.com", "1.1.1.1") is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. No first-registrant auto-admin in production
# ---------------------------------------------------------------------------


def _command(email: str) -> RegisterUserCommand:
    return RegisterUserCommand(
        full_name="Prod Registrant",
        email=email,
        password="Str0ngPassw0rd!",
        birth_date=date(1990, 1, 1),
    )


async def _register_first(settings: Settings) -> UserRole:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            service = AuthService(session, settings=settings)
            result = await service.register(_command("first@example.com"))
            return UserRole(result["user"]["role"])
    finally:
        await engine.dispose()


def test_first_registrant_is_admin_in_development() -> None:
    settings = Settings(environment="development", first_user_admin=True)
    assert asyncio.run(_register_first(settings)) is UserRole.ADMIN


def test_first_registrant_is_not_admin_in_production() -> None:
    # Even with the explicit flag on, production must never self-grant ADMIN.
    settings = Settings(environment="production", first_user_admin=True)
    assert settings.first_registrant_becomes_admin is False
    assert asyncio.run(_register_first(settings)) is UserRole.USER


def test_bootstrap_can_be_disabled_outside_production() -> None:
    settings = Settings(environment="development", first_user_admin=False)
    assert asyncio.run(_register_first(settings)) is UserRole.USER
