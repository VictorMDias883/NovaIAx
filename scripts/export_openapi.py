#!/usr/bin/env python
"""Export the live OpenAPI schema and refresh the endpoint catalog.

Run this after changing any router, schema or response model so the committed
artifacts match the code:

    python scripts/export_openapi.py

It writes two things:

1. ``openapi.json`` — the full schema (imported by tooling and used to generate
   SDKs), with deterministic ordering, two-space indentation and UTF-8 accents
   kept literal so a diff stays readable.
2. The endpoint catalog section of ``API_REFERENCE.md`` — the markdown tables
   between the ``<!-- BEGIN:endpoint-catalog -->`` and
   ``<!-- END:endpoint-catalog -->`` markers, regenerated tag by tag.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Allow ``python scripts/export_openapi.py`` and ``python -m scripts.export_openapi``.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.main import app  # noqa: E402

OPENAPI_OUTPUT = os.path.join(ROOT, "openapi.json")
API_REFERENCE_OUTPUT = os.path.join(ROOT, "API_REFERENCE.md")

_CATALOG_BEGIN = "<!-- BEGIN:endpoint-catalog -->"
_CATALOG_END = "<!-- END:endpoint-catalog -->"
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def _component_name(operation: dict[str, Any], status: str) -> str | None:
    """Return the ``components`` schema name of a response, if referenced."""
    content = operation.get("responses", {}).get(status, {}).get("content", {})
    schema = content.get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    items = schema.get("items", {})
    item_ref = items.get("$ref")
    if isinstance(item_ref, str):
        return f"array[{item_ref.rsplit('/', 1)[-1]}]"
    return None


def _body_name(operation: dict[str, Any]) -> str:
    """Return the request-body schema component name, or ``—``."""
    schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return f"`{ref.rsplit('/', 1)[-1]}`"
    return "JSON" if schema else "—"


def _params(operation: dict[str, Any]) -> str:
    """Render the parameter column (query + path), or ``—``."""
    rendered: list[str] = []
    for param in operation.get("parameters", []):
        name = param.get("name", "")
        location = param.get("in", "")
        schema = param.get("schema", {})
        type_name = schema.get("type") or schema.get("anyOf", [{}])[0].get("type") or "string"
        ref = schema.get("$ref")
        if ref:
            type_name = ref.rsplit("/", 1)[-1]
        required = "required" if param.get("required") else "optional"
        rendered.append(f"`{name}` ({location}, {type_name}, {required})")
    return ", ".join(rendered) if rendered else "—"


def _success(operation: dict[str, Any]) -> str:
    """Render the first 2xx response as ``status `Schema```."""
    for status in sorted(operation.get("responses", {})):
        if not status.startswith("2"):
            continue
        if status == "204":
            return "204 `(no content)`"
        name = _component_name(operation, status)
        return f"{status} `{name or 'JSON'}`"
    return "—"


def _render_catalog(spec: dict[str, Any]) -> str:
    """Render the per-tag markdown tables for the whole schema."""
    grouped: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, operations in spec.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in _HTTP_METHODS:
                continue
            tags = operation.get("tags") or ["other"]
            for tag in tags:
                grouped.setdefault(tag, []).append((method, path, operation))

    blocks: list[str] = []
    for tag in sorted(grouped):
        rows = sorted(grouped[tag], key=lambda item: (item[1], item[0]))
        lines = [
            f"### `{tag}`",
            "",
            "| Method | Path | Summary | Params | Body | Success |",
            "|--------|------|---------|--------|------|---------|",
        ]
        for method, path, operation in rows:
            summary = operation.get("summary") or operation.get("operationId") or ""
            lines.append(
                f"| `{method.upper()}` | `{path}` | {summary} | "
                f"{_params(operation)} | {_body_name(operation)} | {_success(operation)} |"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _update_api_reference(catalog: str) -> None:
    """Splice ``catalog`` between the markers in ``API_REFERENCE.md``."""
    with open(API_REFERENCE_OUTPUT, encoding="utf-8") as handle:
        content = handle.read()
    if _CATALOG_BEGIN not in content or _CATALOG_END not in content:
        print(f"skipped catalog update: markers missing in {API_REFERENCE_OUTPUT}")
        return
    prefix, rest = content.split(_CATALOG_BEGIN, 1)
    _, suffix = rest.split(_CATALOG_END, 1)
    updated = f"{prefix}{_CATALOG_BEGIN}\n{catalog}{_CATALOG_END}{suffix}"
    with open(API_REFERENCE_OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(updated)
    print(f"Updated {API_REFERENCE_OUTPUT} catalog")


def main() -> int:
    spec = app.openapi()
    with open(OPENAPI_OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    print(f"Wrote {OPENAPI_OUTPUT} ({len(spec.get('paths', {}))} paths)")

    _update_api_reference(_render_catalog(spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
