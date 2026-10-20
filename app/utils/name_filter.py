"""
Basic offensive / impersonation filter for user-chosen agent names.

The agent name is shown to the user's assistant and to nothing else, but it is
still user-generated text that should not celebrate harassment or let someone
impersonate the platform ("NovaIAx"), other assistants or well-known brands.

The filter is deliberately *basic*: it normalises the name (case-fold and
strip accents) and rejects it when any whole token matches an entry of a
curated blocklist.  It does not attempt full semantic moderation — that is an
LLM problem — it just closes the obvious holes cheaply and deterministically.
"""

from __future__ import annotations

import re
import unicodedata

#: Offensive terms (PT-BR + EN), normalised (case-folded, accent-stripped).
_OFFENSIVE = frozenset(
    {
        "babaca",
        "besta",
        "bixa",
        "bicha",
        "bobo",
        "boboca",
        "bosta",
        "burro",
        "burra",
        "canalha",
        "chato",
        "chata",
        "cretino",
        "cretina",
        "debil",
        "idiota",
        "imbecil",
        "inutil",
        "escroto",
        "escrota",
        "fdp",
        "filhodaputa",
        "lixo",
        "merda",
        "nojento",
        "nojenta",
        "nojo",
        "otario",
        "otaria",
        "otário",
        "otária",
        "palhaco",
        "palhacinho",
        "palhaço",
        "palhaçinho",
        "piranha",
        "puta",
        "retardado",
        "retardada",
        "trouxa",
        "vagabundo",
        "vagabunda",
        "vadia",
        "viado",
    }
)

#: Platform / assistant impersonation tokens.
_IMPERSONATION = frozenset(
    {
        "alexa",
        "assistente",
        "assistenta",
        "bard",
        "chatgpt",
        "claude",
        "copilot",
        "cortana",
        "gemini",
        "gpt",
        "groq",
        "meta",
        "midjourney",
        "novaiax",
        "openai",
        "sistema",
        "siri",
    }
)

_BLOCKED = frozenset(_OFFENSIVE | _IMPERSONATION)

#: Any run of non-letters/numbers becomes a single space.
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Case-fold and strip accents so matches ignore case and diacritics."""
    deaccented = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    return re.sub(_TOKEN_SPLIT_RE, " ", deaccented.casefold()).strip()


def blocked_name_reason(name: str) -> str | None:
    """Return a reason string when ``name`` is blocked, else ``None``.

    Token-equality matching prevents false positives ("assinatura" contains
    "ass" but shares no whole token with it).  Multi-word names are matched
    token by token; a blocked brand/offensive term anywhere in the name
    rejects the whole thing.
    """
    normalized = _normalize(name)
    if not normalized:
        return "O nome do agente deve ter entre 2 e 30 caracteres."

    tokens = set(normalized.split())
    hit = next((token for token in _BLOCKED if token in tokens), None)
    if hit is not None:
        return f"O nome do agente não pode conter o termo «{hit}»."

    if normalized in {"novaiax assistente", "novaiax agente"}:
        return "O nome do agente não pode se passar por " "plataforma NovaIAx."

    return None
