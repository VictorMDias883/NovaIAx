from dataclasses import dataclass


@dataclass(frozen=True)
class DemoteUserCommand:
    """Command object for demoting a user to USER."""

    target_id: int
