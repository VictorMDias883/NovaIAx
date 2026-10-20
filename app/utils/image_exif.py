"""Best-effort EXIF/metadata stripping for community-report photos.

The project intentionally has **no Pillow dependency**, so this module parses
the container format directly and drops the metadata segments that typically
carry GPS coordinates and device information:

* **JPEG** — removes every ``APP1`` segment (``FFE1``), which is where Exif and
  XMP live, plus ``APP0``/JFIF and ``APP13``/Photoshop segments that can carry
  thumbnails and editing history.  Pixel data is untouched.
* **PNG** — removes ``eXIf``, ``tEXt``, ``iTXt`` and ``zTXt`` chunks.

Anything unrecognised is returned unchanged, so an unsupported format is never
corrupted by a failed parse.
"""

from __future__ import annotations

import struct

_JPEG_APP_SEGMENTS = {0xE0, 0xE1, 0xE2, 0xED}
_PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"iTXt", b"zTXt"}


def _strip_jpeg(data: bytes) -> bytes:
    """Drop APPn metadata segments from a JPEG byte string."""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return data
    out = bytearray(data[:2])  # SOI
    i = 2
    length = len(data)
    while i < length:
        if data[i] != 0xFF:
            out.extend(data[i:])
            break
        marker = data[i + 1]
        # Start of scan / end of image: everything after is compressed data.
        if marker in (0xDA, 0xD9):
            out.extend(data[i:])
            break
        # Standalone markers without a length field.
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out.extend(data[i : i + 2])
            i += 2
            continue
        if i + 4 > length:
            out.extend(data[i:])
            break
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        end = i + 2 + seg_len
        if seg_len < 2 or end > length:
            out.extend(data[i:])
            break
        if marker not in _JPEG_APP_SEGMENTS:
            out.extend(data[i:end])
        i = end
    return bytes(out)


def _strip_png(data: bytes) -> bytes:
    """Drop metadata chunks from a PNG byte string."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return data
    out = bytearray(signature)
    i = len(signature)
    length = len(data)
    while i + 8 <= length:
        chunk_len = struct.unpack(">I", data[i : i + 4])[0]
        chunk_type = data[i + 4 : i + 8]
        total = 12 + chunk_len  # length + type + data + CRC
        if i + total > length:
            out.extend(data[i:])
            break
        if chunk_type not in _PNG_METADATA_CHUNKS:
            out.extend(data[i : i + total])
        i += total
        if chunk_type == b"IEND":
            break
    if i < length:
        out.extend(data[i:])
    return bytes(out)


def strip_metadata(data: bytes) -> bytes:
    """Return ``data`` with image metadata segments removed (best effort)."""
    if data.startswith(b"\xff\xd8"):
        return _strip_jpeg(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _strip_png(data)
    return data


def has_exif_segment(data: bytes) -> bool:
    """Return whether the byte string still contains an Exif/GPS segment.

    Used by tests to assert the stripping worked.
    """
    if data.startswith(b"\xff\xd8"):
        return b"Exif\x00\x00" in data
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return b"eXIf" in data
    return False
