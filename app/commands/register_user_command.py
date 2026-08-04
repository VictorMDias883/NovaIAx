from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterUserCommand:
    full_name: str
    email: str
    password: str
