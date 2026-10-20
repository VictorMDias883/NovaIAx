"""
Small, dependency-free helpers for safely coercing untrusted values to
integers.

Every value that originates outside this process — a JWT claim, an HTTP
header, an AI provider's JSON — arrives with no guarantee about its type.
``int()`` raises ``ValueError`` on anything unexpected, and an unhandled
``ValueError`` inside a request becomes a 500 that tells the caller nothing
useful while hiding the actual fault.  These helpers return ``None``
instead, so each call site can decide what an unusable value means there:
reject the request, skip the field, or fall back to a default.
"""

from typing import Any

__all__ = ["coerce_int"]


def coerce_int(value: Any) -> int | None:
    """Return ``value`` as an ``int``, or ``None`` if it is not a whole number.

    Never raises.

    Accepted inputs:

    * ``int`` — returned as-is.
    * ``bool`` — **rejected** (``None``).  ``bool`` is a subclass of ``int``,
        so ``True`` would otherwise silently become ``1``; a flag landing in
        an ID or count field is a bug worth surfacing.
    * ``float`` — accepted only when it has no fractional part, so ``3.0``
        works while ``3.5`` returns ``None`` rather than silently truncating.
    * ``str`` — parsed, tolerating surrounding whitespace and a leading
        ``+``/``-``.  Strings are accepted because the JWT spec defines
      ``sub`` as a string and some producers quote their numeric claims.

    Anything else — ``None``, ``dict``, ``list``, ``bytes``, … — returns
    ``None``.

    Args:
        value: The value to coerce.

    Returns:
        The value as an ``int``, or ``None`` when it cannot be represented.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
