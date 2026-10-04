"""Attachment types, detected from a file's first bytes."""

from __future__ import annotations

from typing import Optional

#: The API's limit on an attachment's decoded size.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

_SIGNATURES = (
    ("application/pdf", b"%PDF-"),
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
)


def detect_content_type(data: bytes) -> Optional[str]:
    """``application/pdf``, ``image/jpeg`` or ``image/png`` from the first bytes,
    the same check the API makes; ``None`` for anything else. This is not a
    full parse or a malware scan."""
    for content_type, signature in _SIGNATURES:
        if data[: len(signature)] == signature:
            return content_type
    return None


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.2f} MiB"
