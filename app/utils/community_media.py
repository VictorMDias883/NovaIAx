"""Filesystem helpers for community-report photos.

Uploaded photos are written to ``settings.community_media_dir`` with an
EXIF-stripped body and a random ``{user_id}_{uuid}.{ext}`` filename (see
:mod:`app.api.v1.community_router`).  The database only stores that filename,
so deleting a report (or an account) must also unlink the file — otherwise the
bytes outlive the owner's consent.

These helpers are deliberately defensive: a missing file is not an error, the
path is reduced to its basename so it can never escape the media directory, and
any ``OSError`` is logged and swallowed (a cleanup failure must not abort the
user's deletion).
"""

from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


def delete_community_photo(filename: str | None, media_dir: str) -> bool:
    """Delete one stored photo file, returning whether a file was removed.

    ``filename`` is the bare name stored in ``CommunityReport.photo_path``;
    only its basename is used, so a crafted value cannot traverse outside
    ``media_dir``.  Missing files return ``False`` without raising.
    """
    if not filename:
        return False
    base = Path(media_dir).resolve()
    target = (base / Path(filename).name).resolve()
    if target.parent != base:
        logger.warning("Refusing to delete photo outside media dir", extra={"filename": filename})
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - filesystem edge
        logger.warning("Failed to delete community photo", extra={"filename": filename, "error": str(exc)})
        return False
    return True
