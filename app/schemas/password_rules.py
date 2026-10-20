"""
Reusable password-complexity rules for the Pydantic request schemas.

The same policy guards every place a password is *set*: account
registration (:class:`app.schemas.auth.RegisterUserRequest`) and
self-service password rotation
(:class:`app.schemas.user_schemas.PasswordChangeRequest`).  Keeping the
rules in a single mixin means the two endpoints can never drift apart, and
a new password-bearing schema gets the policy just by inheriting from it.

The validator targets both ``password`` and ``new_password`` with
``check_fields=False``, so each concrete schema is validated only on the
field(s) it actually declares.
"""

from pydantic import BaseModel, field_validator


class PasswordRulesMixin(BaseModel):
    """Mixin enforcing the application's password complexity policy.

    A password must contain:
        - At least one digit (0-9).
        - At least one uppercase letter (A-Z).

    Length bounds (8-128 characters) are expressed with
    :class:`~pydantic.Field` on the concrete schemas, since the field name
    differs between them.
    """

    @field_validator("password", "new_password", check_fields=False)
    @classmethod
    def validate_password(cls, value: str) -> str:
        """Enforce password complexity rules.

        This runs while Pydantic parses the request body, so an invalid
        password is rejected with a 422 before it ever reaches the service
        layer.
        """
        if not any(char.isdigit() for char in value):
            raise ValueError("password must contain at least one digit")
        if not any(char.isupper() for char in value):
            raise ValueError("password must contain at least one uppercase letter")
        return value
