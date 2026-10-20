"""
Helpers for reading values out of the authenticated-request context.

Route handlers receive the resolved identity as a plain ``dict`` (from
:func:`app.api.deps.get_current_user`).  The database-backed dependency
validates that the ``id`` claim is a whole number before we get here, but
reading it through :func:`current_user_id` keeps that assumption in one
place instead of re-derived with a bare ``int()`` in every router.
"""

from __future__ import annotations

from typing import Any

from app.utils.coercion import coerce_int

__all__ = ["current_user_id"]


def current_user_id(current_user: dict[str, Any]) -> int:
    """Return the caller's integer user id.

    Args:
        current_user: The identity dict injected by the auth dependency.

    Returns:
        The user's primary key as an ``int``.

    Raises:
        ValueError: When the identity carries no usable ``id`` — a bug in
            the dependency rather than bad input, so it surfaces loudly.
    """
    user_id = coerce_int(current_user.get("id"))
    if user_id is None:
        raise ValueError("authenticated identity has no integer 'id'")
    return user_id
