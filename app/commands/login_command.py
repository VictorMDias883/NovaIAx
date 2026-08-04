"""
Command object for user login.

This module implements the **Command pattern** — a simple, immutable
data container that represents a user's intent to log in.  Using a
dedicated command object (rather than passing raw parameters) makes
the service layer's interface explicit and allows for future
extensions such as validation, logging, or event sourcing.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LoginCommand:
    """Immutable command carrying login credentials.

    Attributes:
        email: The user's email address (used as the login identifier).
        password: The user's plaintext password (will be hashed and
            compared against the stored hash by the service layer).
    """

    email: str
    password: str
