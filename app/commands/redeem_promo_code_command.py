from dataclasses import dataclass


@dataclass(frozen=True)
class RedeemPromoCodeCommand:
    """Command object carrying a user's promo-code redemption request."""

    code: str
    user_id: int
