"""A Play purchase token unlocks exactly one account."""

from uuid import uuid4

import pytest
from app.clients.google_play_client import GoogleSubscriptionState
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.exceptions.plan_errors import PlanErrorCode, PlanRequiredError
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.billing_service import BillingService
from sqlalchemy import delete

_PRODUCT = "meuagente_pro_monthly"


class _FakePlay:
    configured = True

    async def get_subscription_v2(self, product_id: str, purchase_token: str) -> GoogleSubscriptionState:
        return GoogleSubscriptionState(
            product_id=product_id,
            status=SubscriptionStatus.ACTIVE,
            expires_at=None,
            auto_renewing=True,
            is_trial=False,
            acknowledgement_state="ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED",
        )

    async def acknowledge(self, product_id: str, purchase_token: str) -> None:  # pragma: no cover
        return None


@pytest.fixture
def _products(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "google_play_products_json", f'{{"pro": "{_PRODUCT}"}}')


async def _new_user(session) -> int:  # noqa: ANN001
    user = await UserRepository(session).create(
        full_name="Billing Test",
        email=f"billing-{uuid4().hex[:10]}@example.com",
        password_hash="x",
    )
    return user.id


@pytest.mark.usefixtures("_products")
async def test_a_token_already_bound_to_another_account_is_rejected() -> None:
    token = f"token-{uuid4().hex}"
    async with SessionLocal() as session:
        owner_id = await _new_user(session)
        intruder_id = await _new_user(session)
    try:
        async with SessionLocal() as session:
            await BillingService(session, _FakePlay()).verify_and_activate(owner_id, _PRODUCT, token)

        async with SessionLocal() as session:
            with pytest.raises(PlanRequiredError) as exc_info:
                await BillingService(session, _FakePlay()).verify_and_activate(intruder_id, _PRODUCT, token)
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == PlanErrorCode.PURCHASE_INVALID

        # The owner can still re-verify (restore purchases on a new phone).
        async with SessionLocal() as session:
            await BillingService(session, _FakePlay()).verify_and_activate(owner_id, _PRODUCT, token)
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Subscription).where(Subscription.user_id.in_([owner_id, intruder_id])))
            await session.execute(delete(User).where(User.id.in_([owner_id, intruder_id])))
            await session.commit()
