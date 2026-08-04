"""
Pydantic request/response schemas for the **v2** auth endpoints.

These schemas are used by :mod:`app.api.v1.auth_router` — the
database-backed authentication flow that creates real user records
in the database and returns JWT tokens.

Note: There is a separate set of schemas in :mod:`app.schemas.auth`
used by the simpler, in-memory auth flow in :mod:`app.api.v1.auth`.
"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterUserRequest(BaseModel):
    """Request body for the ``POST /auth/register`` endpoint.

    Validates that the provided data meets minimum security requirements
    before it reaches the service layer.

    Attributes:
        full_name: User's display name (2–120 characters).
        email: A valid email address (validated by ``EmailStr``).
        password: Must be 8–128 characters and contain at least one
            digit and one uppercase letter (enforced by
            :meth:`validate_password`).
    """

    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Enforce password complexity rules.

        The password must contain:
            - At least one digit (0–9).
            - At least one uppercase letter (A–Z).

        This validator runs automatically when Pydantic parses the
        request body, so invalid passwords are rejected before they
        reach the service layer.
        """
        if not any(char.isdigit() for char in value):
            raise ValueError("password must contain at least one digit")
        if not any(char.isupper() for char in value):
            raise ValueError("password must contain at least one uppercase letter")
        return value


class LoginRequest(BaseModel):
    """Request body for the ``POST /auth/login`` endpoint.

    Attributes:
        email: The user's email address.
        password: The user's plaintext password (min 1 character).
    """

    email: EmailStr
    password: str = Field(min_length=1)


class AuthResponse(BaseModel):
    """Response body returned by both ``/auth/register`` and ``/auth/login``.

    Attributes:
        user: A dictionary containing the user's ``id``, ``full_name``,
            and ``email``.
        access_token: A short-lived JWT access token.
        refresh_token: A long-lived JWT refresh token.
    """

    user: dict[str, object]
    access_token: str
    refresh_token: str
