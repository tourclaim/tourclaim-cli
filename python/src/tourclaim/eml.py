"""A small ``.eml`` reader: Subject, From, Date, Message-ID and the first
``text/plain`` part.

Header values are kept as written apart from unfolding and RFC 2047 decoding,
so the text sent is the message's own (and a message id derived from it is the
same as the Node edition derives). The MIME structure and transfer encodings
(base64, quoted-printable) are read with :mod:`email.parser`. Anything it cannot
read is reported rather than guessed.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Dict, Optional, Tuple

from .format import iso_utc, parse_iso

_MAX_DEPTH = 10

# The WHATWG Encoding Standard (what browsers and Node's TextDecoder follow)
# reads these labels as windows-1252.
_WINDOWS_1252_LABELS = {
    "ascii", "us-ascii", "iso-8859-1", "iso8859-1", "iso_8859-1", "latin1", "latin-1", "l1",
    "cp819", "ibm819", "iso-ir-100", "csisolatin1", "windows-1252", "cp1252", "x-cp1252",
}


@dataclass
class ParsedEmail:
    subject: Optional[str]
    sender: Optional[str]
    #: ISO 8601 in UTC (``2026-03-03T14:05:00.000Z``), or None without a readable Date.
    date: Optional[str]
    message_id: Optional[str]
    #: The first text/plain part, or None when the message has none.
    text: Optional[str]


def decode_bytes(data: bytes, charset: Optional[str]) -> str:
    label = (charset or "utf-8").strip().lower() or "utf-8"
    if label in _WINDOWS_1252_LABELS:
        label = "cp1252"
    elif label in ("gb2312", "gb_2312", "csgb2312", "x-gbk"):
        label = "gbk"
    try:
        codecs.lookup(label)
    except LookupError:
        label = "utf-8"
    return data.decode(label, errors="replace")


def _split_message(source: str) -> Tuple[str, str]:
    match = re.search(r"\r?\n\r?\n", source)
    if not match:
        return source, ""
    return source[: match.start()], source[match.end():]


def _parse_headers(head: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    unfolded = re.sub(r"\r?\n[ \t]+", " ", head)
    for line in re.split(r"\r?\n", unfolded):
        colon = line.find(":")
        if colon <= 0:
            continue
        name = line[:colon].strip().lower()
        if name not in headers:
            headers[name] = line[colon + 1:].strip()
    return headers


def _b64(data: str) -> bytes:
    cleaned = re.sub(r"[^A-Za-z0-9+/]", "", data)
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned)
    except (binascii.Error, ValueError):
        return b""


def _encoded_word(match: "re.Match[str]") -> str:
    charset, encoding, data = match.group(1), match.group(2), match.group(3)
    if encoding.upper() == "B":
        raw = _b64(data)
    else:
        text = re.sub(r"=([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), data.replace("_", " "))
        raw = text.encode("latin-1", errors="replace")
    return decode_bytes(raw, charset.split("*")[0])


def decode_header(value: str) -> str:
    """Decodes RFC 2047 encoded words. Raw 8-bit header text is read as UTF-8
    when it is valid UTF-8 (``value`` holds the raw bytes as latin-1)."""
    raw = value.encode("latin-1", errors="replace")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = value
    text = re.sub(r"(=\?[^?]+\?[BbQq]\?[^?]*\?=)\s+(?==\?)", r"\1", text)
    return re.sub(r"=\?([^?]+)\?([BbQq])\?([^?]*)\?=", _encoded_word, text)


def _parse_date(value: str) -> Optional[str]:
    moment = None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        moment = parse_iso(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return iso_utc(moment)


def _find_text_part(part: Message, depth: int) -> Optional[str]:
    if depth > _MAX_DEPTH:
        return None
    if part.get_content_maintype() == "multipart":
        payload = part.get_payload()
        if not isinstance(payload, list):
            return None
        for sub in payload:
            if isinstance(sub, Message):
                text = _find_text_part(sub, depth + 1)
                if text is not None:
                    return text
        return None
    if part.get_content_type() != "text/plain":
        return None
    if re.match(r"\s*attachment", str(part.get("Content-Disposition", "")), re.I):
        return None
    data = part.get_payload(decode=True)
    if not isinstance(data, bytes):
        data = b""
    text = decode_bytes(data, part.get_content_charset())
    return text.replace("\r\n", "\n").rstrip()


def parse_eml(raw: bytes) -> ParsedEmail:
    """Reads a saved message."""
    # latin-1 maps each byte to one character, so header bytes survive until
    # they are decoded with their own charset.
    head, _ = _split_message(raw.decode("latin-1"))
    headers = _parse_headers(head)
    message_id = headers.get("message-id", "").strip()
    if message_id.startswith("<"):
        message_id = message_id[1:]
    if message_id.endswith(">"):
        message_id = message_id[:-1]
    message = BytesParser(policy=policy.compat32).parsebytes(raw)
    return ParsedEmail(
        subject=decode_header(headers["subject"]) if "subject" in headers else None,
        sender=decode_header(headers["from"]) if "from" in headers else None,
        date=_parse_date(headers["date"]) if headers.get("date") else None,
        message_id=message_id.strip() or None,
        text=_find_text_part(message, 0),
    )
