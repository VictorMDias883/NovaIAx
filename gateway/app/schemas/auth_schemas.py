from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class RegisterUserRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not any(char.isdigit() for char in value):
            raise ValueError("password must contain at least one digit")
        if not any(char.isupper() for char in value):
            raise ValueError("password must contain at least one uppercase letter")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AuthResponse(BaseModel):
    user: dict[str, object]
    access_token: str
    refresh_token: str
