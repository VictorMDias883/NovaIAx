"""
Server-side verification of Google Play subscriptions.

The mobile app never *proves* a purchase: it only tells us which purchase
token to check.  This client takes that token, asks the Google Play Developer
API for the authoritative ``subscriptionsv2`` state and returns a normalised
:class:`GoogleSubscriptionState` — the billing service is the only thing that
decides entitlement, and only from this answer.

The deployment intentionally carries **no** ``google-*`` dependency: the
service-account access token is minted here with a hand-built RS256 JWT
(:mod:`jose`) exchanged over HTTPS (:mod:`httpx`), and the two API calls this
project needs (``subscriptionsv2.get`` and ``subscriptions.acknowledge``) are
plain REST.  Tests replace :func:`app.api.deps.get_google_play_client` with an
in-memory fake, so nothing here runs in the test suite.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from jose import jwt

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.subscription import SubscriptionStatus

logger = get_logger(__name__)

_API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

#: Google ``subscriptionState`` → our normalised :class:`SubscriptionStatus`.
_STATE_MAP: dict[str, SubscriptionStatus] = {
    "SUBSCRIPTION_STATE_ACTIVE": SubscriptionStatus.ACTIVE,
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": SubscriptionStatus.GRACE,
    "SUBSCRIPTION_STATE_ON_HOLD": SubscriptionStatus.ON_HOLD,
    "SUBSCRIPTION_STATE_PAUSED": SubscriptionStatus.PAUSED,
    "SUBSCRIPTION_STATE_CANCELED": SubscriptionStatus.CANCELED,
    "SUBSCRIPTION_STATE_EXPIRED": SubscriptionStatus.EXPIRED,
    # A pending purchase is not entitled yet; ``on_hold`` is the closest
    # "exists but not usable" state and maps to a 403 at the gate.
    "SUBSCRIPTION_STATE_PENDING": SubscriptionStatus.ON_HOLD,
}


class GooglePlayError(Exception):
    """A purchase could not be verified against the Developer API.

    Args:
        message: Human-readable cause, safe to log (never a token).
        transient: ``True`` when the failure is worth retrying (network,
            5xx, permission/config); ``False`` when the purchase itself is
            unknown/invalid.  The service maps this to 503 vs 400.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class GoogleSubscriptionState:
    """Normalised view of one Play subscription purchase.

    Attributes:
        product_id: The Play product id verified.
        status: Normalised :class:`SubscriptionStatus`.
        expires_at: Entitlement end (Play ``expiryTime``), when reported.
        auto_renewing: Whether Play reports the subscription will renew.
        is_trial: Whether the current offer looks like a free trial.
        acknowledgement_state: Raw Play ``acknowledgementState``.
    """

    product_id: str
    status: SubscriptionStatus
    expires_at: datetime | None
    auto_renewing: bool
    is_trial: bool
    acknowledgement_state: str


class GooglePlayGateway(Protocol):
    """The interface the billing service depends on (faked in tests)."""

    @property
    def configured(self) -> bool:  # pragma: no cover - protocol declaration
        ...

    async def get_subscription_v2(
        self, product_id: str, purchase_token: str
    ) -> GoogleSubscriptionState:  # pragma: no cover - protocol declaration
        ...

    async def acknowledge(
        self, product_id: str, purchase_token: str
    ) -> None:  # pragma: no cover - protocol declaration
        ...


class GooglePlayClient:
    """Talks to the Google Play Developer API on behalf of the deployment."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._access_token: str | None = None
        self._access_token_expiry: float = 0.0

    @property
    def configured(self) -> bool:
        """True when a service-account key is present and parseable."""
        return self._service_account() is not None

    def _service_account(self) -> dict[str, Any] | None:
        """Parse the inline service-account JSON (``None`` when absent/invalid)."""
        raw = self.settings.google_play_service_account_json
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("google_play_service_account_invalid_json")
            return None
        if not isinstance(data, dict) or not data.get("private_key") or not data.get("client_email"):
            logger.warning("google_play_service_account_missing_fields")
            return None
        return data

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=15.0)
        return self._http_client

    async def _token(self) -> str:
        """Return a cached OAuth2 access token, minting one when needed."""
        now = time.time()
        if self._access_token and now < self._access_token_expiry - 30:
            return self._access_token

        account = self._service_account()
        if account is None:
            raise GooglePlayError("Google Play service account is not configured", transient=True)

        token_uri = account.get("token_uri") or _DEFAULT_TOKEN_URI
        issued_at = int(now)
        claims = {
            "iss": account["client_email"],
            "scope": _SCOPE,
            "aud": token_uri,
            "iat": issued_at,
            "exp": issued_at + 3600,
        }
        try:
            assertion = jwt.encode(claims, account["private_key"], algorithm="RS256")
        except Exception as exc:  # noqa: BLE001 - surfaced as a config failure
            raise GooglePlayError("Failed to sign Google service-account assertion", transient=True) from exc

        client = await self._client()
        try:
            response = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise GooglePlayError("Google OAuth token endpoint unreachable", transient=True) from exc
        if response.status_code >= 400:
            raise GooglePlayError(
                f"Google OAuth token request failed ({response.status_code})", transient=True
            )
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise GooglePlayError("Google OAuth response had no access token", transient=True)
        self._access_token = str(token)
        self._access_token_expiry = now + float(payload.get("expires_in", 3600))
        return self._access_token

    async def get_subscription_v2(
        self, product_id: str, purchase_token: str
    ) -> GoogleSubscriptionState:
        """Fetch and normalise the authoritative state for a purchase token."""
        url = (
            f"{_API_BASE}/{self.settings.google_play_package_name}"
            f"/purchases/subscriptionsv2/tokens/{purchase_token}"
        )
        data = await self._request("GET", url, purchase_token)
        return _normalize_subscription(product_id, data)

    async def acknowledge(self, product_id: str, purchase_token: str) -> None:
        """Acknowledge a purchase so Google does not auto-refund it."""
        url = (
            f"{_API_BASE}/{self.settings.google_play_package_name}"
            f"/purchases/subscriptions/{product_id}/tokens/{purchase_token}:acknowledge"
        )
        await self._request("POST", url, purchase_token)

    async def _request(self, method: str, url: str, purchase_token: str) -> dict[str, Any]:
        """Issue an authenticated Developer API request.

        The raw ``purchase_token`` only ever appears in the URL path (to
        Google, as required); it is never written to a log line.
        """
        token = await self._token()
        client = await self._client()
        try:
            response = await client.request(method, url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise GooglePlayError("Google Play API unreachable", transient=True) from exc
        if response.status_code in (401, 403):
            raise GooglePlayError(
                f"Google Play API rejected our credentials ({response.status_code})", transient=True
            )
        if response.status_code == 404:
            raise GooglePlayError("Purchase token not found")
        if response.status_code >= 500:
            raise GooglePlayError(
                f"Google Play API error ({response.status_code})", transient=True
            )
        if response.status_code >= 400:
            raise GooglePlayError(f"Google Play API rejected the request ({response.status_code})")
        if not response.content:
            return {}
        result = response.json()
        return result if isinstance(result, dict) else {}


def _parse_rfc3339(value: Any) -> datetime | None:
    """Parse a Google ``expiryTime`` string into an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _normalize_subscription(product_id: str, data: dict[str, Any]) -> GoogleSubscriptionState:
    """Turn a ``subscriptionsv2`` payload into a :class:`GoogleSubscriptionState`."""
    raw_state = str(data.get("subscriptionState", "")).upper()
    status = _STATE_MAP.get(raw_state, SubscriptionStatus.EXPIRED)

    line_items = data.get("lineItems")
    if not isinstance(line_items, list):
        line_items = []

    expiry: datetime | None = None
    auto_renewing = False
    is_trial = False
    for item in line_items:
        if not isinstance(item, dict):
            continue
        item_expiry = _parse_rfc3339(item.get("expiryTime"))
        if item_expiry is not None and (expiry is None or item_expiry > expiry):
            expiry = item_expiry
        plan = item.get("autoRenewingPlan")
        if isinstance(plan, dict) and plan.get("autoRenewEnabled"):
            auto_renewing = True
        offer = item.get("offerDetails")
        if isinstance(offer, dict):
            haystack = " ".join(
                str(part) for part in (offer.get("basePlanId"), offer.get("offerId"), *offer.get("offerTags", []))
            ).lower()
            if "trial" in haystack:
                is_trial = True

    return GoogleSubscriptionState(
        product_id=product_id,
        status=status,
        expires_at=expiry,
        auto_renewing=auto_renewing,
        is_trial=is_trial,
        acknowledgement_state=str(data.get("acknowledgementState", "")).upper(),
    )
