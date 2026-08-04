"""
Command object for user registration.

Part of the **Command pattern** used throughout the application.
This immutable dataclass encapsulates the data needed to create a
new user account, keeping the service-layer interface clean and
explicit.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RegisterUserCommand:
    """Immutable command carrying new-user registration data.

    Attributes:
        full_name: The user's display name.
        email: The user's unique email address.
        password: The user's plaintext password (will be hashed by
            the service layer before storage).
    """

    full_name: str
    email: str
    password: str
