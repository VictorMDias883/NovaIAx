from dataclasses import dataclass


@dataclass(frozen=True)
class PromoteUserCommand:
    """Command object for promoting a user to ADMIN."""

    target_id: int
