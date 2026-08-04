from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RegisterObjectiveCommand:
    title: str
    description: str | None
    due_date: datetime
