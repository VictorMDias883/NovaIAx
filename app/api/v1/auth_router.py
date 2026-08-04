from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.login_command import LoginCommand
from app.commands.register_user_command import RegisterUserCommand
from app.db.session import SessionLocal
from app.schemas.auth_schemas import AuthResponse, LoginRequest, RegisterUserRequest
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        yield session


@router.post("/register", response_model=AuthResponse)
async def register_user(payload: RegisterUserRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    async for db_session in get_session():
        break
    service = AuthService(db_session)
    command = RegisterUserCommand(full_name=payload.full_name, email=str(payload.email), password=payload.password)
    result = await service.register(command)
    return AuthResponse(**result)


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginRequest, session: AsyncSession = Depends(get_session)) -> AuthResponse:
    async for db_session in get_session():
        break
    service = AuthService(db_session)
    command = LoginCommand(email=str(payload.email), password=payload.password)
    result = await service.login(command)
    return AuthResponse(**result)
