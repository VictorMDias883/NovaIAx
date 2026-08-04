"""
Pydantic request/response schemas for the **v1** auth endpoints.

These schemas are used by :mod:`app.api.v1.auth` — the simpler,
in-memory authentication flow that uses the :class:`AuthService`
from :mod:`app.core.security` (which stores users in memory rather
than in a database).

Note: There is a separate set of schemas in
:mod:`app.schemas.auth_schemas` used by the database-backed v2 auth
flow in :mod:`app.api.v1.auth_router`.
"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Request body for the ``POST /auth/login`` endpoint (v1).

    Unlike the v2 schema, this uses ``username`` instead of ``email``
    because the v1 auth flow authenticates against an in-memory user
    store keyed by username.

    Attributes:
        username: The user's username (min 1 character).
        password: The user's plaintext password (min 1 character).
    """

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    """Request body for the ``POST /auth/refresh`` endpoint.

    Attributes:
        refresh_token: A valid JWT refresh token previously issued by
            the ``/auth/login`` endpoint.
    """

    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """Response body returned by ``/auth/login`` and ``/auth/refresh``.

    Attributes:
        access_token: A short-lived JWT access token.
        refresh_token: A long-lived JWT refresh token.
        token_type: Always ``"bearer"`` — indicates the token type
            for HTTP Authorization header usage.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """Response body returned by the ``GET /auth/me`` endpoint.

    Attributes:
        username: The authenticated user's username (extracted from
            the JWT ``sub`` claim).
        roles: List of role names assigned to the user (e.g.
            ``["admin"]``).
    """

    username: str
    roles: list[str]
