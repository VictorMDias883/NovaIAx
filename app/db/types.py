"""Custom SQLAlchemy column types shared across the application.

The personalization domain stores small JSON lists (e.g. the user's active
group/sub-agent slugs) and the billing domain stores small JSON objects
(sanitized Google Play event payloads) on PostgreSQL in production and SQLite
in tests.  SQLite has no native JSON column, so listing those values as plain
text and (de)serialising them in the mapper keeps one column type working on
both databases while the ORM always hands the service a real ``list``/``dict``.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.types import Text, TypeDecorator


class JSONList(TypeDecorator):
    """Store a Python ``list`` as a JSON-encoded text column.

    ``process_bind_param`` serialises the list before persistence and
    ``process_result_value`` restores it on read; a missing or unparsable
    value maps to an empty list so services never have to guard against
    ``None``.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return None
        return json.dumps(list(value))

    def process_result_value(self, value: Any, dialect: Any) -> list[Any]:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []


class JSONDict(TypeDecorator):
    """Store a Python ``dict`` as a JSON-encoded text column.

    Used by the billing audit trail to persist a *sanitized* copy of a Google
    Play notification.  A missing or unparsable value maps to ``None`` so the
    service can tell "no payload captured" apart from "empty object".
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value: Any, dialect: Any) -> dict[str, Any] | None:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


class JSONValue(TypeDecorator):
    """Store any JSON-able Python value as a JSON-encoded text column.

    Used by the remote app configuration, whose values are heterogeneous
    (booleans, strings, lists and small objects) under a single typed
    registry.  A missing or unparsable value maps to ``None`` so the service
    can fall back to the registry default.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value: Any, dialect: Any) -> Any:  # noqa: ANN401 - SQLAlchemy hook signature
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
