"""
Pydantic request/response schemas for the authentication endpoints.

These schemas are used by :mod:`app.api.v1.auth` — the database-backed
authentication flow that creates real user records in the database and
returns JWT tokens.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import get_settings
from app.schemas.password_rules import PasswordRulesMixin


class RegisterUserRequest(PasswordRulesMixin):
    """Request body for the ``POST /auth/register`` endpoint.

    Validates that the provided data meets minimum security requirements
    before it reaches the service layer.

    Attributes:
        full_name: User's display name (2–120 characters).
        email: A valid email address (validated by ``EmailStr``).
        password: Must be 8–128 characters and contain at least one
            digit and one uppercase letter (enforced by
            :class:`app.schemas.password_rules.PasswordRulesMixin`, shared
            with the password-change endpoint).
        birth_date: The user's date of birth.  Used by the age gate: anyone
            under :attr:`app.core.config.Settings.minimum_account_age_years`
            is refused with a 422 before the account is created.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    birth_date: date

    @field_validator("birth_date")
    @classmethod
    def validate_minimum_age(cls, value: date) -> date:
        """Reject dates that make the applicant younger than the age gate."""
        minimum_age = get_settings().minimum_account_age_years
        today = date.today()
        age_on_today = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age_on_today < minimum_age:
            raise ValueError(
                f"You must be at least {minimum_age} years old to create an account"
            )
        return value


class LoginRequest(BaseModel):
    """Request body for the ``POST /auth/login`` endpoint.

    Attributes:
        email: The user's email address.
        password: The user's plaintext password.

            The upper bound matches :attr:`RegisterUserRequest.password`.  A
            stored password can never exceed 128 characters, so a longer one
            is rejected with a 422 before it reaches the expensive PBKDF2
            hash — without this, a single request could hand the server a
            multi-megabyte "password" and tie up a CPU core for seconds.
    """

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    """Public user representation returned by ``GET /auth/me``."""

    id: int
    full_name: str
    email: str
    role: str


class AuthResponse(BaseModel):
    """Response body returned by both ``/auth/register`` and ``/auth/login``.

    Attributes:
        user: A dictionary containing the user's ``id``, ``full_name``,
            ``email``, and ``role``.
        access_token: A short-lived JWT access token.
        refresh_token: A long-lived JWT refresh token.
    """

    user: UserResponse
    access_token: str
    refresh_token: str


class RefreshRequest(BaseModel):
    """Request body for the ``POST /auth/refresh`` endpoint."""

    refresh_token: str = Field(min_length=1, max_length=4096)


class TokenResponse(BaseModel):
    """Response body returned by the refresh token endpoint."""

    access_token: str
    refresh_token: str


class LogoutRequest(BaseModel):
    """Request body for the ``POST /auth/logout`` endpoint.

    Attributes:
        refresh_token: The refresh token to revoke.
        access_token: Optional access token to revoke as well, so the
            current session ends immediately.
    """

    refresh_token: str = Field(min_length=1, max_length=4096)
    access_token: str | None = Field(default=None, max_length=4096)
