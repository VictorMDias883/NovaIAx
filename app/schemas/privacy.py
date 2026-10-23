"""
Pydantic request/response schemas for the privacy and compliance endpoints.

These back the account self-service routes in :mod:`app.api.v1.privacy_router`:

    - ``GET  /api/v1/me/export`` — no request body, returns the JSON bundle.
    - ``DELETE /api/v1/me`` — requires the account password as proof of
      consent (a leaked access token alone must not be able to wipe data).
    - ``GET /api/v1/me/consents`` / ``PUT /api/v1/me/consents`` /
      ``DELETE /api/v1/me/consents/{type}`` — read, grant/update and
      withdraw the consent-log entries.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.consent import CONSENT_TYPES


class ConsentRecordSchema(BaseModel):
    """One row of the consent log."""

    type: str
    version: str
    accepted_at: datetime


class ConsentListResponse(BaseModel):
    """The caller's full consent log."""

    consents: list[ConsentRecordSchema]


class ConsentUpdateRequest(BaseModel):
    """Grant (or re-acknowledge) one consent type.

    Attributes:
        type: One of ``terms``, ``privacy``, ``microphone``, ``contacts``,
            ``location`` or ``learning``.
        version: The document version being accepted.  Optional — when
            omitted the currently configured version (see
            ``Settings.consent_versions``) is used.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=40)
    version: str | None = Field(default=None, min_length=1, max_length=20)

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        """Only the product's declared consent types are accepted."""
        if value not in CONSENT_TYPES:
            raise ValueError(f"unknown consent type '{value}'")
        return value


class AccountDeletionRequest(BaseModel):
    """Request body for ``DELETE /api/v1/me``.

    ``password`` proves the caller is the account owner, mirroring the
    self-service password-change rule (a wrong password is a 400, never an
    ambiguous 401).
    """

    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=128)


class AccountDeletionResponse(BaseModel):
    """Result of an account deletion.

    Always includes a confirmation message; ``subscription_management_url``
    is present when the account had a Play subscription, pointing the user at
    Google's own cancellation screen.
    """

    message: str
    subscription_management_url: str | None = None


class ConsentWithdrawalResponse(BaseModel):
    """Result of withdrawing one consent type via ``DELETE /me/consents/{type}``."""

    message: str


class ExportedUser(BaseModel):
    """The account record embedded in a data-export bundle.

    Deliberately excludes the password hash and any purchase-token hash.
    """

    id: int
    full_name: str
    email: str
    role: str
    birth_date: date | None = None
    created_at: datetime


class ExportedUserAgent(BaseModel):
    """The optional personalization profile embedded in a data-export bundle."""

    agent_name: str
    tone: str | None = None
    language: str
    timezone: str
    active_group_slugs: list[str]
    active_sub_agent_slugs: list[str]
    simplified_mode: bool
    learning_enabled: bool
    created_at: datetime


class ExportedSubscription(BaseModel):
    """The optional Play subscription embedded in a data-export bundle."""

    plan: str
    status: str
    product_id: str | None = None
    expires_at: datetime | None = None
    auto_renewing: bool
    is_trial: bool
    created_at: datetime


class DataExportResponse(BaseModel):
    """A portable, machine-readable export of everything tied to the account.

    The stable top-level sections are typed; the per-module row arrays
    (``objectives``, ``memories``, ``productivity``, …) are pre-serialized
    JSON-safe documents, so they are declared as plain mappings while still
    surfacing a non-empty schema in the OpenAPI document.  ``extra="allow"``
    keeps any future section from being silently dropped by the response
    model.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int
    generated_at: datetime
    user: ExportedUser
    consents: list[ConsentRecordSchema]
    consent_history: list[dict[str, Any]]
    objectives: list[dict[str, Any]]
    user_agent: ExportedUserAgent | None = None
    memories: list[dict[str, Any]]
    agent_actions: list[dict[str, Any]]
    ai_feedback: list[dict[str, Any]]
    chat_messages: list[dict[str, Any]]
    conversations: dict[str, list[dict[str, str]]]
    promo_redemptions: list[dict[str, Any]]
    subscription: ExportedSubscription | None = None
    purchase_events: list[dict[str, Any]]
    productivity: dict[str, list[dict[str, Any]]]
    health: dict[str, list[dict[str, Any]]]
    finance: dict[str, list[dict[str, Any]]]
    study: dict[str, list[dict[str, Any]]]
    logbook: dict[str, list[dict[str, Any]]]
    community: dict[str, list[dict[str, Any]]]
    third_party_processors: list[dict[str, Any]]

