"""
Shared helpers for extracting JSON payloads from AI (LLM) replies.

Language models rarely emit a bare JSON document: they wrap it in a markdown
fence, prefix it with "Aqui está:" or append a sign-off sentence.  Every
AI-backed feature therefore needs the same three-step recovery, which used to
be copy-pasted per service.  :func:`extract_json` is the single
implementation; :mod:`app.services.objective_service` and
:mod:`app.services.objective_assistant_service` both delegate to it.
"""

import json
import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Matches a markdown code fence, with or without a language tag.
_FENCE_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)

#: How much of a failing reply is attached to the warning log record.
_LOG_PREVIEW_CHARS = 500


def extract_json(
    text: str,
    *,
    context: str = "AI response",
    warn_on_plain_text: bool = False,
) -> Any:
    """Attempt to parse JSON out of an AI reply.

    Tries, in order:

    1. the whole message as-is;
    2. the contents of a ```` ```json ... ``` ```` code fence;
    3. the substring between the first ``{`` and the last ``}``.

    Args:
        text: The raw reply from the AI provider.
        context: Short label identifying the caller, included in the warning
            message so log records stay attributable after the merge.
        warn_on_plain_text: When ``True``, a reply that contains no JSON at
            all is also logged as a warning.  Leave it ``False`` (the default)
            for conversational flows, where answering in prose is the expected
            behaviour and logging every turn would be noise; set it to
            ``True`` for flows that strictly require a JSON document and
            treat prose as a contract violation.

    Returns:
        The parsed value on success, or ``None`` when no valid JSON could be
        extracted.  A parsed value is *not* guaranteed to have the expected
        shape — callers must still validate it against their own schema.
    """
    if not isinstance(text, str):
        return None

    # 1. The whole message.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. A markdown code fence.
    fence = _FENCE_RE.search(text)
    if fence is not None:
        body = fence.group("body").strip()
        if body:
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass

    # 3. The first object-looking substring.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    _warn_unparsable(text, context=context, warn_on_plain_text=warn_on_plain_text)
    return None


def _warn_unparsable(text: str, *, context: str, warn_on_plain_text: bool) -> None:
    """Log a single warning when a reply yielded no JSON.

    A reply containing a brace was *trying* to emit JSON and failed, so it is
    always worth reporting.  A reply without any brace is the model simply
    answering in prose, which is only surprising when the caller demanded a
    JSON document (``warn_on_plain_text``).
    """
    looks_like_json = "{" in text
    if not (warn_on_plain_text or looks_like_json):
        return
    logger.warning(
        f"{context}: no JSON payload could be extracted from the AI reply",
        extra={
            "ai_response": text[:_LOG_PREVIEW_CHARS],
            "looks_like_json": looks_like_json,
        },
    )
