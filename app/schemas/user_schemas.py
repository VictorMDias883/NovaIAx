from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.password_rules import PasswordRulesMixin


class UserResponse(BaseModel):
    """Public user representation returned by admin endpoints."""

    id: int
    full_name: str
    email: str
    role: str
    created_at: datetime


class UserProfileResponse(BaseModel):
    """Public identity of the signed-in user.

    Returned by the self-service endpoints (``PATCH /users/me``).  Unlike
    :class:`UserResponse` it omits ``created_at``, so the shape matches
    exactly what ``GET /auth/me`` and the login/register responses carry —
    a client can therefore use it to refresh its cached profile in place.
    """

    id: int
    full_name: str
    email: str
    role: str


class UserProfileUpdate(BaseModel):
    """Request body for the ``PATCH /users/me`` endpoint.

    Only the display name is editable.  The e-mail is deliberately
    immutable here: it is both the login identifier and the unique account
    key, so changing it is an identity change rather than a profile edit and
    is intentionally left out of scope.

    Attributes:
        full_name: The new display name (2–120 characters), matching the
            bounds enforced at registration.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=120)


class PasswordChangeRequest(PasswordRulesMixin):
    """Request body for the ``PATCH /users/me/password`` endpoint.

    Rotating a password requires proving ownership of the current one, so
    both values are mandatory.  ``new_password`` inherits the shared
    complexity policy (8–128 characters, at least one digit and one
    uppercase letter).

    Attributes:
        current_password: The account's existing plaintext password,
            verified against the stored hash by the service layer.
        new_password: The replacement password.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128)


class UserListResponse(BaseModel):
    """Paginated listing of users returned by ``GET /users``."""

    users: list[UserResponse]
    page: int
    limit: int
    total: int


class ActionResponse(BaseModel):
    """Generic response body for promote/demote actions."""

    message: str
