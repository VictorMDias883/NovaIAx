"""
Integration tests for user self-service (:mod:`app.api.v1.user_router`).

These tests exercise the account-management endpoints available to *any*
authenticated user acting on their own account:

1. **Profile** — ``PATCH /api/v1/users/me`` renames the caller and returns
   the refreshed identity in the same shape as ``GET /auth/me``.
2. **Password change** — ``PATCH /api/v1/users/me/password`` rotates the
   caller's password, invalidates previously issued refresh tokens, and
   rejects a wrong current password, a reused password, and a weak one.

Every user created here is removed afterwards so the shared ``test.db``
stays deterministic across runs.
"""

import asyncio

import pytest
from app.db.session import SessionLocal
from app.main import app
from app.models.user import User, UserRole
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

_PASSWORD = "Password123"
_NEW_PASSWORD = "BrandNew456"

_API = "/api/v1"


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
            await session.commit()


def _register_and_login(client: TestClient, created_emails: list[str], email: str) -> str:
    """Register ``email``, log in, and return the access token.

    Registration is idempotent here: a 409 means the account survived from a
    previous run, so the login is attempted either way.
    """
    created_emails.append(email)
    client.post(
        f"{_API}/auth/register",
        json={"full_name": "Self Service User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    login = client.post(f"{_API}/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _full_name_of(email: str) -> str:
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return str(result.scalars().one().full_name)


# ---------------------------------------------------------------------------
# PATCH /api/v1/users/me
# ---------------------------------------------------------------------------


def test_update_profile_renames_the_caller(client: TestClient, created_emails: list[str]) -> None:
    """The caller's display name is updated and echoed back in the response."""
    token = _register_and_login(client, created_emails, "self-profile@example.com")

    response = client.patch(f"{_API}/users/me", json={"full_name": "Renamed Person"}, headers=_auth(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["full_name"] == "Renamed Person"
    # The response is interchangeable with GET /auth/me, so a client can
    # replace its cached profile wholesale.
    assert set(body) == {"id", "full_name", "email", "role"}
    assert body["email"] == "self-profile@example.com"


@pytest.mark.asyncio
async def test_update_profile_persists_and_is_visible_on_auth_me(client: TestClient, created_emails: list[str]) -> None:
    """The new name survives in the database and on the next session lookup."""
    token = _register_and_login(client, created_emails, "self-profile-persist@example.com")

    client.patch(f"{_API}/users/me", json={"full_name": "Persisted Name"}, headers=_auth(token))

    assert await _full_name_of("self-profile-persist@example.com") == "Persisted Name"
    me = client.get(f"{_API}/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["full_name"] == "Persisted Name"


def test_update_profile_rejects_a_missing_or_short_name(client: TestClient, created_emails: list[str]) -> None:
    """The name is required and must respect the 2-120 character bounds."""
    token = _register_and_login(client, created_emails, "self-profile-bounds@example.com")

    empty = client.patch(f"{_API}/users/me", json={"full_name": ""}, headers=_auth(token))
    too_short = client.patch(f"{_API}/users/me", json={"full_name": "A"}, headers=_auth(token))
    missing = client.patch(f"{_API}/users/me", json={}, headers=_auth(token))
    too_long = client.patch(f"{_API}/users/me", json={"full_name": "x" * 121}, headers=_auth(token))

    for response in (empty, too_short, missing, too_long):
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": "Requisição inválida.", "code": "validation_error"}


def test_update_profile_forbids_extra_fields(client: TestClient, created_emails: list[str]) -> None:
    """Only ``full_name`` is editable — the e-mail and role cannot be smuggled in.

    The e-mail is the login identifier and the unique account key, and the
    role is what the admin promote/demote endpoints exist for, so both must
    be rejected rather than silently ignored.
    """
    token = _register_and_login(client, created_emails, "self-profile-extra@example.com")

    for payload in (
        {"full_name": "Fine Name", "email": "hijack@example.com"},
        {"full_name": "Fine Name", "role": "ADMIN"},
    ):
        response = client.patch(f"{_API}/users/me", json=payload, headers=_auth(token))
        assert response.status_code == 422, response.text

    me = client.get(f"{_API}/auth/me", headers=_auth(token))
    assert me.json()["email"] == "self-profile-extra@example.com"
    assert me.json()["role"] != "ADMIN"


def test_update_profile_requires_authentication(client: TestClient) -> None:
    """An anonymous call is rejected by the auth middleware."""
    response = client.patch(f"{_API}/users/me", json={"full_name": "Anonymous"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Autenticação necessária.", "code": "authentication_required"}


def test_self_service_routes_reject_api_key_identities() -> None:
    """API-key (``SERVICE``) identities have no database row to edit.

    ``require_db_user`` must turn them into a 403 rather than letting the
    service layer try to coerce the sentinel id ``"api-key"`` into an int.
    """
    from app.api.deps import require_db_user
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(require_db_user({"id": "api-key", "role": UserRole.SERVICE.value}))

    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# PATCH /api/v1/users/me/password
# ---------------------------------------------------------------------------


def test_change_password_rotates_the_credentials(client: TestClient, created_emails: list[str]) -> None:
    """After the change the old password fails and the new one works."""
    token = _register_and_login(client, created_emails, "self-password@example.com")

    change = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers=_auth(token),
    )
    assert change.status_code == 200, change.text
    assert change.json() == {"message": "Password updated."}

    old = client.post(f"{_API}/auth/login", json={"email": "self-password@example.com", "password": _PASSWORD})
    assert old.status_code == 401
    assert old.json() == {"detail": "Credenciais inválidas.", "code": "invalid_credentials"}

    new = client.post(f"{_API}/auth/login", json={"email": "self-password@example.com", "password": _NEW_PASSWORD})
    assert new.status_code == 200, new.text


def test_change_password_invalidates_earlier_refresh_tokens(client: TestClient, created_emails: list[str]) -> None:
    """A refresh token minted before the change can no longer mint a new pair.

    Bumping ``refresh_token_valid_after`` is what makes every other signed-in
    session of this account sign out.
    """
    email = "self-password-refresh@example.com"
    created_emails.append(email)
    client.post(
        f"{_API}/auth/register",
        json={"full_name": "Refresh User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    login = client.post(f"{_API}/auth/login", json={"email": email, "password": _PASSWORD})
    token = str(login.json()["access_token"])
    refresh_token = str(login.json()["refresh_token"])

    change = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers=_auth(token),
    )
    assert change.status_code == 200, change.text

    refresh = client.post(f"{_API}/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401
    assert refresh.json() == {"detail": "Token de atualização inválido.", "code": "authentication_required"}


def test_change_password_rejects_a_wrong_current_password_with_400(
    client: TestClient, created_emails: list[str]
) -> None:
    """A wrong current password is a 400 — never a 401.

    A 401 here would be indistinguishable from an expired access token to
    the client, which is free to react by signing the user out.  A typo must
    cost one error message, not the session.  The stored password is left
    untouched.
    """
    token = _register_and_login(client, created_emails, "self-password-wrong@example.com")

    response = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": "NotMyPassword1", "new_password": _NEW_PASSWORD},
        headers=_auth(token),
    )

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "A senha atual está incorreta.", "code": "bad_request"}

    # The original password still works, i.e. nothing was written.
    still_valid = client.post(
        f"{_API}/auth/login", json={"email": "self-password-wrong@example.com", "password": _PASSWORD}
    )
    assert still_valid.status_code == 200


def test_change_password_rejects_reusing_the_current_password(client: TestClient, created_emails: list[str]) -> None:
    """Changing to the same value is refused instead of silently 'succeeding'."""
    token = _register_and_login(client, created_emails, "self-password-same@example.com")

    response = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": _PASSWORD, "new_password": _PASSWORD},
        headers=_auth(token),
    )

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "A nova senha deve ser diferente da senha atual.", "code": "bad_request"}


def test_change_password_enforces_the_shared_complexity_policy(client: TestClient, created_emails: list[str]) -> None:
    """The new password must satisfy the same rules as at registration.

    The policy is shared with ``RegisterUserRequest`` via
    :class:`app.schemas.password_rules.PasswordRulesMixin`, so the two
    endpoints cannot drift apart.
    """
    token = _register_and_login(client, created_emails, "self-password-policy@example.com")

    for new_password in ("Short1", "nouppercase1", "NoDigitsHere", "x" * 129):
        response = client.patch(
            f"{_API}/users/me/password",
            json={"current_password": _PASSWORD, "new_password": new_password},
            headers=_auth(token),
        )
        assert response.status_code == 422, (new_password, response.text)
        assert response.json() == {"detail": "Requisição inválida.", "code": "validation_error"}


def test_change_password_forbids_extra_fields(client: TestClient, created_emails: list[str]) -> None:
    """Extra fields are rejected so nothing unexpected is written."""
    token = _register_and_login(client, created_emails, "self-password-extra@example.com")

    response = client.patch(
        f"{_API}/users/me/password",
        json={
            "current_password": _PASSWORD,
            "new_password": _NEW_PASSWORD,
            "role": "ADMIN",
        },
        headers=_auth(token),
    )

    assert response.status_code == 422, response.text


def test_change_password_requires_authentication(client: TestClient) -> None:
    """An anonymous call is rejected by the auth middleware."""
    response = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Autenticação necessária.", "code": "authentication_required"}


def test_self_service_is_not_gated_on_the_admin_role(client: TestClient, created_emails: list[str]) -> None:
    """A plain USER (not ADMIN) can edit their own account.

    Guards against the self-service routes accidentally inheriting the
    ``require_admin_db_role`` dependency used by the administration routes
    sharing the same router.
    """
    token = _register_and_login(client, created_emails, "self-role@example.com")

    me = client.get(f"{_API}/auth/me", headers=_auth(token))
    assert me.json()["role"] == UserRole.USER.value

    profile = client.patch(f"{_API}/users/me", json={"full_name": "Regular User"}, headers=_auth(token))
    assert profile.status_code == 200, profile.text

    password = client.patch(
        f"{_API}/users/me/password",
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers=_auth(token),
    )
    assert password.status_code == 200, password.text
