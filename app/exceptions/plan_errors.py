"""
Typed, machine-readable failures for plan gating and Google Play billing.

Just like :mod:`app.exceptions.ai_errors`, the mobile app must be able to
react to a paywall differently from a transient failure, so these errors
carry a stable ``code``:

    ===========================  ======  ===============================
    ``code``                     status  when it happens
    ===========================  ======  ===============================
    ``plan_upgrade_required``    402     The feature exists but not in the
                                         user's current plan.
    ``plan_feature_locked``      402     Alias used when a feature is
                                         entirely absent from the plan.
    ``plan_limit_reached``       402     A countable ceiling (groups,
                                         sub-agents, device commands) was
                                         exceeded.
    ``subscription_inactive``    403     The user has a subscription but it
                                         is not entitled (on hold, paused,
                                         expired, revoked).
    ``billing_unavailable``      503     Play billing is not configured or
                                         the Developer API is unreachable.
    ``purchase_invalid``         400     The purchase token could not be
                                         verified or maps to no known plan.
    ===========================  ======  ===============================

All messages are pt-BR because the app shows them verbatim when it has no
localised copy.
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


class PlanErrorCode(str, Enum):
    """Stable identifiers for plan-gating and billing failures."""

    UPGRADE_REQUIRED = "plan_upgrade_required"
    FEATURE_LOCKED = "plan_feature_locked"
    LIMIT_REACHED = "plan_limit_reached"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    BILLING_UNAVAILABLE = "billing_unavailable"
    PURCHASE_INVALID = "purchase_invalid"


DEFAULT_PLAN_MESSAGES: dict[PlanErrorCode, str] = {
    PlanErrorCode.UPGRADE_REQUIRED: (
        "Este recurso não está incluído no seu plano atual. "
        "Faça upgrade para continuar usando."
    ),
    PlanErrorCode.FEATURE_LOCKED: (
        "Este recurso não está disponível no seu plano. Faça upgrade para desbloquear."
    ),
    PlanErrorCode.LIMIT_REACHED: "Você atingiu o limite do seu plano atual. Faça upgrade para ampliar.",
    PlanErrorCode.SUBSCRIPTION_INACTIVE: (
        "Sua assinatura não está ativa no momento. "
        "Renove sua assinatura para voltar a usar este recurso."
    ),
    PlanErrorCode.BILLING_UNAVAILABLE: (
        "A verificação de compras está indisponível no momento. Tente novamente mais tarde."
    ),
    PlanErrorCode.PURCHASE_INVALID: (
        "Não foi possível validar sua compra. "
        "Confirme a compra no Google Play e tente novamente."
    ),
}

_STATUS_BY_CODE: dict[PlanErrorCode, int] = {
    PlanErrorCode.UPGRADE_REQUIRED: 402,
    PlanErrorCode.FEATURE_LOCKED: 402,
    PlanErrorCode.LIMIT_REACHED: 402,
    PlanErrorCode.SUBSCRIPTION_INACTIVE: 403,
    PlanErrorCode.BILLING_UNAVAILABLE: 503,
    PlanErrorCode.PURCHASE_INVALID: 400,
}


class PlanRequiredError(HTTPException):
    """A plan/billing failure carrying a stable, app-mappable ``code``.

    Args:
        code: The :class:`PlanErrorCode` identifying the failure.
        detail: pt-BR message shown to the user.  Defaults to the message
            registered for ``code``.
        feature: The gated feature that triggered the failure, when relevant.
        current_plan: The plan the user is currently entitled to.
        required_plan: The cheapest plan that would unlock ``feature``.
        limit: The numeric ceiling that was exceeded, when relevant.
    """

    def __init__(
        self,
        code: PlanErrorCode,
        *,
        detail: str | None = None,
        feature: str | None = None,
        current_plan: str | None = None,
        required_plan: str | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.feature = feature
        self.current_plan = current_plan
        self.required_plan = required_plan
        self.limit = limit
        super().__init__(
            status_code=_STATUS_BY_CODE[code],
            detail=detail or DEFAULT_PLAN_MESSAGES[code],
        )
