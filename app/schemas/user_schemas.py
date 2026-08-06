from datetime import datetime

from pydantic import BaseModel


class UserResponse(BaseModel):
    """Public user representation returned by admin endpoints."""

    id: int
    full_name: str
    email: str
    role: str
    created_at: datetime


class UserListResponse(BaseModel):
    """Paginated listing of users returned by ``GET /users``."""

    users: list[UserResponse]
    page: int
    limit: int
    total: int


class ActionResponse(BaseModel):
    """Generic response body for promote/demote actions."""

    message: str
