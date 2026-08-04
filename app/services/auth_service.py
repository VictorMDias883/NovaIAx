from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.login_command import LoginCommand
from app.commands.register_user_command import RegisterUserCommand
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.repositories.user_repository import UserRepository

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, command: RegisterUserCommand) -> dict[str, Any]:
        repo = UserRepository(self.session)
        existing = await repo.get_by_email(command.email)
        if existing:
            raise HTTPException(status_code=409, detail="User already exists")

        password_hash = pwd_context.hash(command.password)
        user = await repo.create(full_name=command.full_name, email=command.email, password_hash=password_hash)
        return {
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "email": user.email,
            },
            "access_token": create_access_token(str(user.id)),
            "refresh_token": create_refresh_token(str(user.id)),
        }

    async def login(self, command: LoginCommand) -> dict[str, Any]:
        repo = UserRepository(self.session)
        user = await repo.get_by_email(command.email)
        if not user or not pwd_context.verify(command.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        return {
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "email": user.email,
            },
            "access_token": create_access_token(str(user.id)),
            "refresh_token": create_refresh_token(str(user.id)),
        }
