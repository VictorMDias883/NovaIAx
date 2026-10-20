"""
Typed, machine-readable failures for promo-code redemption.

The app needs to react differently to "this code does not exist" versus "you
already used it", so each failure carries a **stable** code the client can
switch on.  The HTTP body is ``{"detail": {"code": <stable>, "message": <pt-BR>}}``
so the app has both a machine value and human copy.

    ===========================  ======  ===============================
    ``code``                     status  when it happens
    ===========================  ======  ===============================
    ``not_found``                404     No such code (or it is disabled).
    ``expired``                  410     Outside the validity window.
    ``exhausted``                409     Global redemption ceiling reached.
    ``already_redeemed``         409     The caller already used this code.
    ===========================  ======  ===============================
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException


class PromoErrorCode(str, Enum):
    """Stable identifiers for promo-redemption failures."""

    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    ALREADY_REDEEMED = "already_redeemed"


DEFAULT_PROMO_MESSAGES: dict[PromoErrorCode, str] = {
    PromoErrorCode.NOT_FOUND: "Código promocional não encontrado.",
    PromoErrorCode.EXPIRED: "Este código promocional expirou.",
    PromoErrorCode.EXHAUSTED: "Este código promocional esgotou.",
    PromoErrorCode.ALREADY_REDEEMED: "Você já resgatou este código.",
}

_STATUS_BY_CODE: dict[PromoErrorCode, int] = {
    PromoErrorCode.NOT_FOUND: 404,
    PromoErrorCode.EXPIRED: 410,
    PromoErrorCode.EXHAUSTED: 409,
    PromoErrorCode.ALREADY_REDEEMED: 409,
}


class PromoRedeemError(HTTPException):
    """A promo-redemption failure carrying a stable, app-mappable ``code``."""

    def __init__(self, code: PromoErrorCode, *, message: str | None = None) -> None:
        self.code = code
        super().__init__(
            status_code=_STATUS_BY_CODE[code],
            detail={"code": code.value, "message": message or DEFAULT_PROMO_MESSAGES[code]},
        )


__all__ = ["PromoErrorCode", "PromoRedeemError", "DEFAULT_PROMO_MESSAGES"]
