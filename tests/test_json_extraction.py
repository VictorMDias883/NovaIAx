"""Tests for the shared :func:`app.utils.json_extraction.extract_json` helper.

The helper replaced two identical private copies that previously lived inside
the objective and objective-assistant services, so its behaviour is pinned
here: three extraction strategies, warning semantics, and failure handling.
"""

import logging

import pytest
from app.utils.json_extraction import extract_json


def test_plain_json_object_is_parsed() -> None:
    assert extract_json('{"reply": "oi", "action": null}') == {"reply": "oi", "action": None}


def test_json_array_is_parsed() -> None:
    assert extract_json('[1, 2, 3]') == [1, 2, 3]


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '{"a": 1}',
    ],
)
def test_fenced_json_is_parsed(raw: str) -> None:
    assert extract_json(raw) == {"a": 1}


def test_json_embedded_in_surrounding_prose_is_found() -> None:
    raw = 'Claro! Aqui está: {"reply": "ok", "action": null} — espero ter ajudado.'
    assert extract_json(raw) == {"reply": "ok", "action": None}


def test_multiple_json_documents_fail_closed() -> None:
    """A reply carrying two documents is not recovered — it returns ``None``.

    Strategy 3 spans the first ``{`` to the last ``}``, so a reply with two
    concatenated documents yields an unparsable slice.  Failing closed is the
    intended outcome: callers then treat the turn as "no structured data"
    rather than acting on a guess.  This also matches the behaviour of the two
    private implementations this helper replaced.
    """
    assert extract_json('{"a": 1} e depois {"b": 2}') is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "apenas texto sem json",
        "{isso nao eh json}",
        "{'a': 1}",  # single quotes are not valid JSON
        '{"a": 1',  # truncated
        "{",
        "}",
        "```json\n{quebrado\n```",
    ],
)
def test_invalid_json_returns_none(raw: str) -> None:
    assert extract_json(raw) is None


def test_valid_json_value_that_is_not_an_object_is_returned_verbatim() -> None:
    """The helper parses JSON, it does not assume a shape."""
    assert extract_json("42") == 42
    assert extract_json("true") is True
    assert extract_json('"texto"') == "texto"


def test_warning_is_logged_when_plain_text_is_requested(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.utils.json_extraction"):
        assert extract_json("nada de json aqui", warn_on_plain_text=True) is None
    assert any("no JSON payload could be extracted" in r.message for r in caplog.records)


def test_no_warning_when_plain_text_warning_is_disabled(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.utils.json_extraction"):
        assert extract_json("nada de json aqui") is None
    assert caplog.records == []


def test_braced_garbage_still_warns_in_default_mode(caplog: pytest.LogCaptureFixture) -> None:
    """The default heuristic warns whenever the text looks like it *tried* JSON."""
    with caplog.at_level(logging.WARNING, logger="app.utils.json_extraction"):
        assert extract_json("{isso nao eh json}") is None
    assert any("no JSON payload could be extracted" in r.message for r in caplog.records)


def test_successful_parse_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.utils.json_extraction"):
        assert extract_json('{"a": 1}', warn_on_plain_text=True) == {"a": 1}
    assert caplog.records == []


def test_context_appears_in_the_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The context string is what makes these warnings actionable in logs."""
    with caplog.at_level(logging.WARNING, logger="app.utils.json_extraction"):
        extract_json("no json", context="Objective assistant", warn_on_plain_text=True)
    assert any("Objective assistant" in r.message for r in caplog.records)
