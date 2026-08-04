"""
Command object for objective registration.

Part of the **Command pattern** used throughout the application.
This immutable dataclass encapsulates the data needed to create a
new objective (task/goal) for a user.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RegisterObjectiveCommand:
    """Immutable command carrying new-objective registration data.

    Attributes:
        title: Short title for the objective.
        description: Optional longer description.
        due_date: Deadline for the objective (timezone-aware datetime).
    """

    title: str
    description: str | None
    due_date: datetime
