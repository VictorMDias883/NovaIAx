"""
ETag / ``Cache-Control`` helpers for the public catalog endpoints.

The ``/agent-groups``, ``/sub-agents`` and agents list are read-mostly catalog
data: clients may cache them, but an admin edit must be picked up promptly.
A strong ``ETag`` (SHA-256 of the canonical JSON body) lets a client revalidate
with ``If-None-Match`` and receive a bodyless ``304`` when nothing changed,
while a short ``max-age`` keeps staleness bounded.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import Response


def _canonical_body(payload: Any) -> bytes:
    """Serialise JSON-able data deterministically (stable key order)."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def cached_json_response(
    request: Request,
    payload: Any,
    *,
    max_age: int = 60,
) -> Response:
    """Return a JSON :class:`Response` with ``ETag``/``Cache-Control`` headers.

    When the caller's ``If-None-Match`` matches the computed tag, a bodyless
    ``304 Not Modified`` is returned instead.
    """
    body = _canonical_body(payload)
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    headers = {
        "ETag": etag,
        "Cache-Control": f"public, max-age={max_age}",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)
