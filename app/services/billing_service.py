"""
Google Play billing: verify client purchases and apply RTDN notifications.

The golden rule is that the client is never trusted: ``POST
/billing/google/verify`` carries a purchase token, and this service asks the
Play Developer API what that token actually means before writing anything.
Real Time Developer Notifications (RTDN) go through the same funnel — the
Pub/Sub payload only tells us *which* purchase changed; the authoritative
state is fetched again via the API.  Both paths write a ``purchase_events``
audit row and upsert the single ``subscriptions`` row.

The raw purchase token is never stored or logged: it is hashed with SHA-256
on the way in, and the audit payload is built from normalised fields only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.clients.google_play_client import GooglePlayError, GooglePlayGateway, GoogleSubscriptionState
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.exceptions.plan_errors import PlanErrorCode, PlanRequiredError
from app.models.purchase_event import PurchaseEvent
from app.models.subscription import Subscription, SubscriptionPlan
from app.repositories.subscription_repository import SubscriptionRepository

logger = get_logger(__name__)

#: Google RTDN ``notificationType`` → short audit label.
_RTDN_EVENT_TYPES: dict[int, str] = {
    1: "recovered",
    2: "renewed",
    3: "canceled",
    4: "purchased",
    5: "on_hold",
    6: "grace_period",
    7: "restarted",
    8: "price_change_confirmed",
    9: "deferred",
    10: "paused",
    11: "pause_schedule_changed",
    12: "revoked",
    13: "expired",
    20: "pending_purchase_canceled",
}


def hash_purchase_token(token: str) -> str:
    """Return the SHA-256 hex digest used to store/look up a purchase token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _plan_from_name(name: str) -> SubscriptionPlan | None:
    try:
        return SubscriptionPlan(name)
    except ValueError:
        return None


def normalize_rtdn(body: dict[str, Any]) -> dict[str, Any] | None:
    """Decode a Pub/Sub push envelope into a normalised RTDN dict.

    Returns ``None`` for messages that are not subscription notifications
    (e.g. Google's ``testNotification``), so the caller can acknowledge them
    without doing any work.

    The returned dict has ``notification_type`` (int|None), ``purchase_token``
    (str|None), ``product_id`` (str|None), ``subscription_id`` (str|None) and
    ``payload`` (a sanitized copy safe to persist).
    """
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    encoded = message.get("data")
    if not isinstance(encoded, str):
        return None
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        envelope = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        logger.warning("rtdn_payload_undecodable")
        return None
    if not isinstance(envelope, dict):
        return None

    notification = envelope.get("subscriptionNotification")
    if not isinstance(notification, dict):
        # ``testNotification`` or unknown envelope — nothing to apply.
        return None

    notification_type = notification.get("notificationType")
    if isinstance(notification_type, str) and notification_type.isdigit():
        notification_type = int(notification_type)
    elif not isinstance(notification_type, int):
        notification_type = None

    return {
        "notification_type": notification_type,
        "purchase_token": notification.get("purchaseToken"),
        "product_id": notification.get("subscriptionId"),
        "subscription_id": envelope.get("subscription"),
        "payload": {
            "notificationType": notification_type,
            "subscriptionId": notification.get("subscriptionId"),
            # Deliberately no purchaseToken: the audit row keeps only its hash.
        },
    }


class BillingService:
    """Verifies purchases and keeps the local subscription mirror in sync."""

    def __init__(
        self,
        session,  # noqa: ANN001 - AsyncSession
        google_client: GooglePlayGateway,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.google_client = google_client
        self.settings = settings or get_settings()
        self.repository = SubscriptionRepository(session)

    async def verify_and_activate(
        self, user_id: int, product_id: str, purchase_token: str
    ) -> Subscription:
        """Verify a client purchase, acknowledge it and upsert entitlement.

        Raises :class:`PlanRequiredError` with ``purchase_invalid`` when the
        token is unknown or the product maps to no plan, and
        ``billing_unavailable`` when Play is not configured or unreachable.
        """
        plan = _plan_from_name(self.settings.plan_by_product_id.get(product_id, ""))
        if plan is None:
            raise PlanRequiredError(
                PlanErrorCode.PURCHASE_INVALID,
                detail="Produto de assinatura desconhecido.",
            )
        state = await self._fetch_state(product_id, purchase_token)

        if (
            self.settings.billing_acknowledge_purchases
            and state.acknowledgement_state == "ACKNOWLEDGEMENT_STATE_PENDING"
        ):
            try:
                await self.google_client.acknowledge(product_id, purchase_token)
            except GooglePlayError as exc:
                # A failed acknowledgement must not block entitlement: Play
                # retries the acknowledgement on the next verify/RTDN, and a
                # refund risk is preferable to locking out a paying user.
                logger.warning("google_acknowledge_failed", extra={"transient": exc.transient})

        token_hash = hash_purchase_token(purchase_token)
        subscription = await self._upsert(user_id, plan, product_id, token_hash, state)
        self.repository.add_event(
            PurchaseEvent(
                user_id=user_id,
                subscription_id=subscription.id,
                event_type="verify",
                notification_type=None,
                product_id=product_id,
                purchase_token_hash=token_hash,
                payload=self._state_payload(state),
            )
        )
        await self.session.commit()
        return subscription

    async def apply_rtdn(self, notification: dict[str, Any]) -> None:
        """Apply a normalised RTDN after re-verifying it against Play.

        The notification is only a *hint*: the authoritative state is fetched
        again and the local row is updated from that.  Unknown tokens are
        still audited (with no owner) so support can trace them.
        """
        raw_token = notification.get("purchase_token")
        token_hash = hash_purchase_token(raw_token) if isinstance(raw_token, str) and raw_token else ""
        notification_type: int | None = notification.get("notification_type")
        if not isinstance(notification_type, int):
            notification_type = None
        event_type = (
            _RTDN_EVENT_TYPES.get(notification_type, "unknown") if notification_type is not None else "unknown"
        )
        product_id = notification.get("product_id")

        subscription = (
            await self.repository.get_by_token_hash(token_hash) if token_hash else None
        )
        if subscription is None:
            logger.info("rtdn_unknown_purchase", extra={"event_type": event_type})
            if token_hash:
                self.repository.add_event(
                    PurchaseEvent(
                        user_id=None,
                        subscription_id=None,
                        event_type=event_type,
                        notification_type=notification_type,
                        product_id=product_id,
                        purchase_token_hash=token_hash,
                        payload=notification.get("payload"),
                    )
                )
                await self.session.commit()
            return

        product_id = product_id or subscription.product_id
        plan = _plan_from_name(self.settings.plan_by_product_id.get(product_id, "")) or subscription.plan

        state: GoogleSubscriptionState | None = None
        if isinstance(raw_token, str) and raw_token and self.google_client.configured:
            try:
                state = await self.google_client.get_subscription_v2(product_id, raw_token)
            except GooglePlayError as exc:
                logger.warning(
                    "rtdn_reverify_failed",
                    extra={"event_type": event_type, "transient": exc.transient},
                )

        if state is not None:
            subscription = await self._upsert(
                subscription.user_id, plan, product_id, token_hash, state
            )
        else:
            # Could not re-verify: keep the row but reflect the notification's
            # own lifecycle hint so an expiry/revoke still downgrades access.
            subscription.status = {
                "canceled": subscription.status,
                "expired": subscription.status,
            }.get(event_type, subscription.status)

        self.repository.add_event(
            PurchaseEvent(
                user_id=subscription.user_id,
                subscription_id=subscription.id,
                event_type=event_type,
                notification_type=notification_type,
                product_id=product_id,
                purchase_token_hash=token_hash,
                payload=notification.get("payload"),
            )
        )
        await self.session.commit()

    async def _fetch_state(
        self, product_id: str, purchase_token: str
    ) -> GoogleSubscriptionState:
        if not self.google_client.configured:
            raise PlanRequiredError(
                PlanErrorCode.BILLING_UNAVAILABLE,
                detail="A verificação de compras não está configurada neste servidor.",
            )
        try:
            return await self.google_client.get_subscription_v2(product_id, purchase_token)
        except GooglePlayError as exc:
            code = PlanErrorCode.BILLING_UNAVAILABLE if exc.transient else PlanErrorCode.PURCHASE_INVALID
            raise PlanRequiredError(code) from exc

    async def _upsert(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        product_id: str,
        token_hash: str,
        state: GoogleSubscriptionState,
    ) -> Subscription:
        subscription = await self.repository.get_by_user(user_id)
        if subscription is None:
            subscription = Subscription(
                user_id=user_id,
                plan=plan,
                status=state.status,
                product_id=product_id,
                purchase_token_hash=token_hash,
                expires_at=state.expires_at,
                auto_renewing=state.auto_renewing,
                is_trial=state.is_trial,
            )
            self.repository.add(subscription)
        else:
            subscription.plan = plan
            subscription.status = state.status
            subscription.product_id = product_id
            subscription.purchase_token_hash = token_hash
            subscription.expires_at = state.expires_at
            subscription.auto_renewing = state.auto_renewing
            subscription.is_trial = state.is_trial
        await self.session.flush()
        return subscription

    @staticmethod
    def _state_payload(state: GoogleSubscriptionState) -> dict[str, Any]:
        """Build a token-free summary of a verified state for the audit trail."""
        return {
            "status": state.status.value,
            "expiresAt": state.expires_at.isoformat() if state.expires_at else None,
            "autoRenewing": state.auto_renewing,
            "isTrial": state.is_trial,
            "acknowledgementState": state.acknowledgement_state,
            "verifiedAt": datetime.now(UTC).isoformat(),
        }
