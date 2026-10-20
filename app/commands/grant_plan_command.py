from dataclasses import dataclass


@dataclass(frozen=True)
class GrantPlanCommand:
    """Command object for an admin granting a plan to a user directly."""

    user_id: int
    plan: str
    duration_days: int
    reason: str
    admin_id: int | None = None
