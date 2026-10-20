"""String slugification helpers."""

from __future__ import annotations

import re
import unicodedata

#: Any run of non-alphanumerics becomes a single underscore.
_SEP_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_length: int = 64, sep: str = "_") -> str:
    """Slugify ``text`` (lowercased, accent-stripped, alphanumerics only).

    Leading/trailing separators are stripped so the result is safe to use as
    a URL or a stable key.  Empty input yields an empty string.
    """
    deaccented = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    slug = re.sub(_SEP_RE, sep, deaccented.casefold()).strip(sep)
    return slug[:max_length].rstrip(sep)
