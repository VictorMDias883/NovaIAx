from datetime import datetime

from pydantic import BaseModel, Field


class RegisterObjectiveRequest(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    due_date: datetime


class ObjectiveResponse(BaseModel):
    id: int
    title: str
    description: str | None
    due_date: datetime
    user_id: int
