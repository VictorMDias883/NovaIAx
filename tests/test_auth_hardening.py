"""Tests for the authentication and authorization hardening.

Covers:

1. **Duplicate registration race** — two concurrent registrations of the
   same address must not surface as a 500; the loser gets a 409.
2. **Guarded JWT claim parsing** — a malformed ``sub``/``iat``/``exp``
   yields a clean 401 instead of an unhandled ``ValueError``.
3. **Constant-time master-key comparison** — ``compare_digest`` is used,
   and matching/non-matching still behave correctly.
4. **Login password length cap** — an over-long password is rejected by
   validation, before any hashing work.
5. **Request-scoped identity reuse** — the middleware and the dependency
   share one authentication, so a protected request loads the user once.
6. **``/redoc`` is public** — like ``/docs``, it needs no credentials.
"""

import asyncio
from datetime import date

import pytest
from app.api.deps import get_current_user
from app.commands.register_user_command import RegisterUserCommand
from app.core.config import Settings
from app.core.security import ApiKeyService, claim_int, coerce_int, create_access_token
from app.db.session import Base
from app.models.user import User
from app.schemas.auth import LoginRequest
from app.services.auth_service import AuthService, _is_unique_violation
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def _run(scenario) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await scenario(session)
    finally:
        await engine.dispose()


def _command(email: str = "Race@Example.com") -> RegisterUserCommand:
    return RegisterUserCommand(
        full_name="Race Tester",
        email=email,
        password="Str0ngPassw0rd!",
        birth_date=date(1990, 1, 1),
    )


# ---------------------------------------------------------------------------
# 1. Duplicate registration race
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_registration_returns_409(monkeypatch) -> None:
    """A unique-constraint violation must be reported as a conflict, not a 500.

    Two requests can both pass the "does this email exist?" check before
    either inserts.  On the backend that loses the race the insert fails on
    the unique index; without this handler the user sees a 500 for what is,
    from their point of view, a straightforward duplicate account.
    """
    from fastapi import HTTPException

    async def scenario(session) -> None:
        service = AuthService(session)
        await service.register(_command("first@example.com"))

        # Simulate the loser of the race: the pre-check saw nothing, and the
        # INSERT trips the unique index.
        from app.repositories.user_repository import UserRepository

        original_create = UserRepository.create

        async def losing_create(self, **kwargs):
            user = await original_create(self, **kwargs)
            await self.session.flush()
            # Force the same violation a concurrent insert would raise.
            await self.session.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO users (full_name, email, password_hash, role, "
                    "refresh_token_valid_after, created_at) VALUES ('x', :email, 'x', "
                    "'USER', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"email": kwargs["email"]},
            )
            return user

        monkeypatch.setattr(UserRepository, "create", losing_create)

        with pytest.raises(HTTPException) as exc:
            await service.register(_command("second@example.com"))

        assert exc.value.status_code == 409
        assert exc.value.detail == "Usuário já existe."

    asyncio.run(_run(scenario))


def test_existing_email_still_returns_409_before_inserting() -> None:
    """The pre-check path is unchanged for the ordinary duplicate."""
    from fastapi import HTTPException

    async def scenario(session) -> None:
        service = AuthService(session)
        await service.register(_command())
        with pytest.raises(HTTPException) as exc:
            await service.register(_command())
        assert exc.value.status_code == 409

    asyncio.run(_run(scenario))


def test_only_unique_violations_are_treated_as_conflicts() -> None:
    """Other integrity problems must stay 500s so they remain visible."""
    not_null = IntegrityError("INSERT", {}, Exception("NOT NULL constraint failed: users.email"))
    foreign_key = IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))
    unique = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: users.email"))
    pg_unique = IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))

    assert _is_unique_violation(unique)
    assert _is_unique_violation(pg_unique)
    assert not _is_unique_violation(not_null)
    assert not _is_unique_violation(foreign_key)


def test_session_is_usable_after_a_rejected_duplicate() -> None:
    """A rejected registration must not leave the session in a bad state."""
    from fastapi import HTTPException

    async def scenario(session) -> None:
        service = AuthService(session)
        await service.register(_command())

        with pytest.raises(HTTPException):
            await service.register(_command())

        # A different email still registers on the same session.
        result = await service.register(_command("other@example.com"))
        assert result["user"]["email"] == "other@example.com"

    asyncio.run(_run(scenario))


def test_duplicate_check_runs_after_the_table_lock() -> None:
    """The check must not happen before the lock, or the race reopens."""
    calls: list[str] = []
    from app.repositories.user_repository import UserRepository

    original_lock = UserRepository.lock_users_table
    original_get = UserRepository.get_by_email

    async def spy_lock(self) -> None:
        calls.append("lock")
        await original_lock(self)

    async def spy_get(self, email):
        calls.append("get_by_email")
        return await original_get(self, email)

    UserRepository.lock_users_table = spy_lock  # type: ignore[method-assign]
    UserRepository.get_by_email = spy_get  # type: ignore[method-assign]

    async def scenario(session) -> None:
        await AuthService(session).register(_command())

    try:
        asyncio.run(_run(scenario))
    finally:
        UserRepository.lock_users_table = original_lock  # type: ignore[method-assign]
        UserRepository.get_by_email = original_get  # type: ignore[method-assign]

    assert calls[:2] == ["lock", "get_by_email"], (
        "the duplicate check must be performed inside the locked section"
    )


# ---------------------------------------------------------------------------
# 2. Guarded JWT claim parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "claim", "expected"),
    [
        ({"sub": "42"}, "sub", 42),
        ({"sub": 42}, "sub", 42),
        ({"sub": " 42 "}, "sub", 42),
        ({"sub": "abc"}, "sub", None),
        ({"sub": None}, "sub", None),
        ({"sub": True}, "sub", None),
        ({"sub": "api-key"}, "sub", None),
        ({"sub": [1]}, "sub", None),
        ({"sub": 3.5}, "sub", None),
        ({"sub": 3.0}, "sub", 3),
        ({}, "sub", None),
    ],
)
def test_coerce_int_accepts_only_whole_numbers(payload, claim, expected) -> None:
    """``coerce_int`` is total: it returns a value or ``None``, never raises."""
    assert coerce_int(payload.get(claim)) == expected
    assert claim_int(payload, claim) == expected


def test_refresh_rejects_a_non_numeric_subject_with_401() -> None:
    """A tampered ``sub`` must be rejected cleanly, not crash with 500."""
    from fastapi import HTTPException
    from jose import jwt

    async def scenario(session) -> None:
        service = AuthService(session)
        settings = Settings(
            secret_key="test-secret-key-that-is-long-enough-for-hs256",
        )
        # Sign with the *service's* algorithm/secret so decoding succeeds and
        # only the claim's shape is wrong.
        forged = jwt.encode(
            {
                "sub": "not-a-number",
                "type": "refresh",
                "jti": "abc",
                "iat": 1,
                "exp": 4_102_444_800,
            },
            settings.secret_key,
            algorithm=settings.jwt_algorithm,
        )
        assert service is not None

        with pytest.raises(HTTPException) as exc:
            await service.refresh(forged)

        assert exc.value.status_code == 401

    asyncio.run(_run(scenario))


def test_refresh_rejects_a_non_numeric_exp_with_401() -> None:
    """A malformed ``exp`` must not reach ``int()`` unguarded."""
    from fastapi import HTTPException
    from jose import jwt

    async def scenario(session) -> None:
        user = User(
            full_name="Test",
            email="exp@example.com",
            password_hash="x",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        settings = Settings(secret_key="test-secret-key-that-is-long-enough-for-hs256")
        forged = jwt.encode(
            {
                "sub": str(user.id),
                "type": "refresh",
                "jti": "abc",
                "iat": 1,
                "exp": "soon",
            },
            settings.secret_key,
            algorithm=settings.jwt_algorithm,
        )

        service = AuthService(session)
        # ``decode_token`` in the service uses the global settings, so patch
        # the secret it verifies with.
        import app.services.auth_service as module

        original = module.decode_token
        module.decode_token = lambda token, *a, **k: jwt.decode(  # type: ignore[assignment]
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
        try:
            with pytest.raises(HTTPException) as exc:
                await service.refresh(forged)
        finally:
            module.decode_token = original  # type: ignore[assignment]

        assert exc.value.status_code == 401

    asyncio.run(_run(scenario))


# ---------------------------------------------------------------------------
# 3. Constant-time master-key comparison
# ---------------------------------------------------------------------------


def test_master_key_is_compared_in_constant_time() -> None:
    """``compare_digest`` must be the comparison, not ``==``."""
    import app.core.security as security

    calls: list[tuple[bytes, bytes]] = []
    original = security.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return original(a, b)

    security.secrets.compare_digest = spy  # type: ignore[assignment]
    try:
        service = ApiKeyService(
            settings=Settings(master_api_key="super-secret-master-key"),
        )
        assert asyncio.run(service.authenticate_api_key("super-secret-master-key")) is True
        assert asyncio.run(service.authenticate_api_key("wrong-key")) is False
    finally:
        security.secrets.compare_digest = original  # type: ignore[assignment]

    assert len(calls) == 2, "both attempts must go through the constant-time comparison"
    assert calls[0][0] == b"super-secret-master-key"


def test_empty_or_blank_api_keys_are_rejected() -> None:
    """An empty key can never match an unset master key."""
    service = ApiKeyService(settings=Settings(master_api_key=""))
    assert asyncio.run(service.authenticate_api_key("")) is False
    assert asyncio.run(service.authenticate_api_key("   ")) is False


# ---------------------------------------------------------------------------
# 4. Login password length cap
# ---------------------------------------------------------------------------


def test_login_rejects_an_over_long_password() -> None:
    """A huge password is rejected by validation, before any hashing."""
    with pytest.raises(ValidationError):
        LoginRequest(email="user@example.com", password="x" * 129)

    assert LoginRequest(email="user@example.com", password="x" * 128).password == "x" * 128


def test_login_keeps_its_one_character_minimum() -> None:
    """The lower bound is unchanged: a 1-character password is still valid."""
    assert LoginRequest(email="user@example.com", password="x").password == "x"


def test_login_cap_matches_the_registration_cap() -> None:
    """Both endpoints must agree on the maximum password length."""
    from app.schemas.auth import RegisterUserRequest

    # Must satisfy the shared password policy too (uppercase + digit), so a
    # rejection can only be attributed to the length.
    at_limit = "A1" + "a" * 126
    over_limit = "A1" + "a" * 127
    assert len(at_limit) == 128 and len(over_limit) == 129

    def accepts(model, value: str, **extra) -> bool:
        try:
            model(password=value, **extra)
        except ValidationError:
            return False
        return True

    common = {"email": "user@example.com"}
    assert accepts(LoginRequest, at_limit, **common)
    assert not accepts(LoginRequest, over_limit, **common)
    assert accepts(RegisterUserRequest, at_limit, **common, full_name="User", birth_date="1990-01-01")
    assert not accepts(RegisterUserRequest, over_limit, **common, full_name="User", birth_date="1990-01-01"), (
        "login and registration must agree: no stored password can exceed 128 characters"
    )


# ---------------------------------------------------------------------------
# 5. Request-scoped identity reuse
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in exposing only what ``get_current_user`` touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.scope: dict[str, object] = {}


def test_identity_resolved_once_is_reused(monkeypatch) -> None:
    """The second call must not re-decode the token or reload the user."""
    loads: list[int] = []

    from app.repositories.user_repository import UserRepository

    original = UserRepository.get_by_id

    async def counting(self, user_id):
        loads.append(user_id)
        return await original(self, user_id)

    monkeypatch.setattr(UserRepository, "get_by_id", counting)

    settings = Settings(secret_key="test-secret-key-that-is-long-enough-for-hs256")
    import app.api.deps as deps

    original_decode = deps.decode_token
    deps.decode_token = lambda token, *a, **k: {  # type: ignore[assignment]
        "sub": "1",
        "type": "access",
        "role": "USER",
    }

    async def scenario() -> None:
        request = _FakeRequest({"Authorization": "Bearer whatever"})
        first = await get_current_user(
            request=request,
            api_key_service=ApiKeyService(settings=Settings(master_api_key="")),
            session=None,
            denylist=None,
        )
        assert loads == [1]
        second = await get_current_user(
            request=request,
            api_key_service=ApiKeyService(settings=Settings(master_api_key="")),
            session=None,
            denylist=None,
        )
        assert first == second
        assert loads == [1], "the user must be loaded once per request"

    try:
        asyncio.run(scenario())
    finally:
        deps.decode_token = original_decode  # type: ignore[assignment]

    assert settings is not None


def test_a_second_user_gets_its_own_identity(monkeypatch) -> None:
    """The cache is per-request, never shared between requests."""
    from tests.helpers import force_memory_redis

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with factory() as session:
                session.add_all(
                    [
                        User(full_name="One", email="one@example.com", password_hash="x"),
                        User(full_name="Two", email="two@example.com", password_hash="x"),
                    ]
                )
                await session.commit()

                import app.api.deps as deps

                original_decode = deps.decode_token
                original_factory = deps.SessionLocal
                ids = iter([1, 2])
                deps.decode_token = lambda token, *a, **k: {  # type: ignore[assignment]
                    "sub": str(next(ids)),
                    "type": "access",
                    "role": "USER",
                }
                deps.SessionLocal = factory  # type: ignore[assignment]
                try:
                    first_request = _FakeRequest({"Authorization": "Bearer a"})
                    second_request = _FakeRequest({"Authorization": "Bearer b"})
                    first = await get_current_user(request=first_request, session=None)
                    second = await get_current_user(request=second_request, session=None)
                finally:
                    deps.decode_token = original_decode  # type: ignore[assignment]
                    deps.SessionLocal = original_factory  # type: ignore[assignment]

                assert first["email"] == "one@example.com"
                assert second["email"] == "two@example.com"
        finally:
            await engine.dispose()

    force_memory_redis(monkeypatch)
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. /redoc is public
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/redoc", "/redoc/"])
def test_redoc_is_a_public_path(path) -> None:
    """ReDoc is rendered from the public OpenAPI schema, like /docs."""
    from app.middlewares.auth_middleware import _is_public_path

    assert _is_public_path(path)


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/health", "/api/v1/auth/login"])
def test_other_public_paths_stay_public(path) -> None:
    from app.middlewares.auth_middleware import _is_public_path

    assert _is_public_path(path)


@pytest.mark.parametrize("path", ["/redocs", "/redocx", "/api/v1/objectives/"])
def test_redoc_prefix_does_not_over_match(path) -> None:
    """The prefix must not leak authentication onto unrelated paths."""
    from app.middlewares.auth_middleware import _is_public_path

    assert not _is_public_path(path)


def test_access_token_claims_are_integers() -> None:
    """The issued tokens keep the numeric claims the parsers now expect."""
    from app.core.security import decode_token

    token = create_access_token("7", email="a@b.com", role="USER")
    payload = decode_token(token)
    assert claim_int(payload, "sub") == 7
    assert claim_int(payload, "iat") is not None
    assert claim_int(payload, "exp") is not None
