"""
Minimal RFC 6238 TOTP implementation (no third-party dependency).

The admin console optionally protects logins with a time-based one-time
password that any standard authenticator app (Google Authenticator, Aegis,
1Password, …) can generate from the ``otpauth://`` provisioning URI.  The
algorithm is small enough to implement directly on the standard library,
which avoids pulling an extra runtime dependency into the image.

References:
    * RFC 4226 — HOTP (the HMAC counter core).
    * RFC 6238 — TOTP (the time-based profile of HOTP).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

#: TOTP parameters (the widely deployed defaults: 6 digits, 30 s step).
DIGITS = 6
PERIOD = 30
_SECRET_BYTES = 20


def generate_secret() -> str:
    """Return a fresh base32-encoded TOTP secret (160 bits, unpadded)."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 secret, tolerating missing padding and lowercase input."""
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding)


def _hotp(secret: str, counter: int) -> str:
    """Compute the 6-digit HOTP value for ``counter`` (RFC 4226 §5.3)."""
    key = _decode_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def current_code(secret: str, at: float | None = None) -> str:
    """Return the code valid at ``at`` (defaults to now); handy for tests."""
    now = time.time() if at is None else at
    return _hotp(secret, int(now) // PERIOD)


def verify(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Return whether ``code`` is valid for ``secret`` at ``at``.

    A window of ``1`` accepts the previous, current and next step, which
    absorbs the small clock skew common between server and phone.  The
    comparison is constant-time to avoid leaking the expected digits.
    """
    if not secret or not code:
        return False
    candidate = code.strip().replace(" ", "")
    if not candidate.isdigit():
        return False
    now = time.time() if at is None else at
    counter = int(now) // PERIOD
    return any(
        hmac.compare_digest(_hotp(secret, counter + offset), candidate)
        for offset in range(-window, window + 1)
    )


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """Build the ``otpauth://`` URI an authenticator app scans/enters."""
    label = quote(f"{issuer}:{account}", safe="")
    params = f"secret={secret}&issuer={quote(issuer, safe='')}&digits={DIGITS}&period={PERIOD}"
    return f"otpauth://totp/{label}?{params}"


__all__ = ["DIGITS", "PERIOD", "current_code", "generate_secret", "provisioning_uri", "verify"]
