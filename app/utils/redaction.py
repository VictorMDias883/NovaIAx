"""
Redaction helpers for the admin audit trail.

The audit log records *what changed*, not the data itself.  Before a
before/after snapshot is written it is passed through :func:`redact`, which:

* drops any key whose name looks like a **secret** (password, token, hash,
  API key, TOTP secret, …) entirely, and
* replaces any key whose name looks like **free-text personal content**
  (message body, note, phone, address, coordinates, …) with the placeholder
  ``"[redigido]"``.

The matching is deliberately name-based and conservative: an unknown key is
kept, because the audit trail is only ever fed a small, operator-defined set
of metadata fields, never arbitrary user payloads.
"""

from __future__ import annotations

from typing import Any

#: Substrings that mark a key as a secret; the value is removed.
_SECRET_MARKERS = (
    "password",
    "senha",
    "secret",
    "segredo",
    "token",
    "hash",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "totp",
    "private",
)

#: Substrings that mark a key as personal content; the value is masked.
_PERSONAL_MARKERS = (
    "content",
    "conteudo",
    "body",
    "corpo",
    "message",
    "mensagem",
    "description",
    "descricao",
    "note",
    "anotacao",
    "title",
    "titulo",
    "text",
    "texto",
    "phone",
    "telefone",
    "address",
    "endereco",
    "location",
    "local",
    "latitude",
    "longitude",
    "lat",
    "lng",
    "email",
    "birth",
    "nascimento",
)

MASK = "[redigido]"


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _is_personal(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _PERSONAL_MARKERS)


def redact(value: Any) -> Any:
    """Return a recursively redacted copy of ``value``.

    Mappings lose their secret keys and have personal keys masked; sequences
    are redacted element-wise; everything else is returned unchanged.
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                cleaned[str(key)] = redact(item)
            elif _is_secret(key):
                continue
            elif _is_personal(key):
                cleaned[key] = MASK
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


__all__ = ["MASK", "redact"]
