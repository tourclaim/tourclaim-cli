"""Times and human-readable output."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

_ISO = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]+))?)?)?"
    r"(Z|[+-][0-9]{2}:?[0-9]{2})?",
    re.I,
)


def parse_iso(value: str, default_utc: bool = True) -> Optional[datetime]:
    """Parses an ISO 8601 date or date and time into an aware UTC datetime.
    A value without a zone is UTC when ``default_utc`` (the API sends some
    timestamps that way, for example key expiry ``2026-11-03T18:00:00``)."""
    match = _ISO.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    try:
        micro = int((fraction or "0")[:6].ljust(6, "0"))
        result = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), micro)
    except ValueError:
        return None
    if zone is None:
        if not default_utc:
            return None
        return result.replace(tzinfo=timezone.utc)
    if zone.upper() == "Z":
        offset = timedelta(0)
    else:
        digits = zone[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        if zone[0] == "-":
            offset = -offset
    try:
        return result.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_server_time(value: Optional[str]) -> Optional[datetime]:
    """A server timestamp as an aware UTC datetime, or ``None``."""
    if not value or not isinstance(value, str):
        return None
    parsed = parse_iso(value)
    if parsed is not None:
        return parsed
    try:
        fallback = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if fallback is None:
        return None
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=timezone.utc)
    return fallback.astimezone(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """``2026-03-03T14:05:00.000Z``, the form JavaScript's toISOString writes."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + f".{utc.microsecond // 1000:03d}Z"


def format_time(value: Optional[str]) -> str:
    """``2026-11-03 18:00 UTC``."""
    moment = parse_server_time(value)
    if moment is None:
        return value or "unknown"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _round(x: float) -> int:
    return int(math.floor(x + 0.5))


def relative_time(value: Optional[str], now: float) -> str:
    """``in 3 days``, ``in 5 hours``, ``2 days ago``. ``now`` is a Unix time in seconds."""
    moment = parse_server_time(value)
    if moment is None:
        return ""
    diff = moment.timestamp() - now
    magnitude = abs(diff)
    hour = 3600.0
    day = 24 * hour
    if magnitude < 60:
        return "just now"

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit}{'' if n == 1 else 's'}"

    if magnitude >= day:
        amount = plural(_round(magnitude / day), "day")
    elif magnitude >= hour:
        amount = plural(_round(magnitude / hour), "hour")
    else:
        amount = plural(_round(magnitude / 60), "minute")
    return f"in {amount}" if diff >= 0 else f"{amount} ago"


def mode_notice(mode: Optional[str]) -> Optional[str]:
    if mode == "review":
        return "review mode: claims are synthetic and nothing is filed"
    if mode == "live":
        return "live: submitted claims go to Copernican for review and filing"
    return None


_STATE_TEXT = {
    "collecting": "collecting (answers are missing)",
    "needs_approval": "needs_approval (complete; the traveler must review and sign)",
    "ready_to_submit": "ready_to_submit (signed; ready to submit)",
    "submitted": "submitted (read-only; a claim exists)",
}


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return value.replace("\n", "\n" + " " * 26)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def intake_lines(intake: Mapping[str, Any], title: str = "Draft") -> List[str]:
    """Human summary of an intake."""
    lines = [f"{title} {intake.get('id')}"]

    def pad(label: str) -> str:
        return "  " + label.ljust(10) + " "

    state = intake.get("state")
    lines.append(pad("State:") + _STATE_TEXT.get(str(state), str(state)))
    lines.append(pad("Revision:") + str(intake.get("revision")))
    notice = mode_notice(intake.get("mode"))
    if notice:
        lines.append(pad("Mode:") + notice)
    lines.append(pad("Coverage:") + "not determined (nothing here decides whether the loss is covered)")
    evidence = intake.get("evidence") or []
    if evidence:
        lines.append(pad("Evidence:") + ", ".join(str(e.get("label")) for e in evidence))
    else:
        lines.append(pad("Evidence:") + "none yet")

    saved: List[tuple] = []
    for key, value in (intake.get("fields") or {}).items():
        if value is None:
            continue
        if key == "medical" and isinstance(value, dict):
            for m_key, m_value in value.items():
                if m_value is not None:
                    saved.append((f"medical.{m_key}", m_value))
        else:
            saved.append((key, value))
    if saved:
        lines += ["", "Saved answers:"]
        for key, value in saved:
            lines.append(f"  {key.ljust(24)}{_value_text(value)}")
    missing = intake.get("missing_fields") or []
    if missing:
        lines += ["", f"Missing ({len(missing)}): {', '.join(missing)}"]
    questions = intake.get("next_questions") or []
    if questions:
        lines += ["", "Ask the traveler next:"]
        lines += [f"  {i}. {q}" for i, q in enumerate(questions, 1)]
    reason = (intake.get("fields") or {}).get("reason_category")
    if reason in ("ILLNESS", "INJURY") and intake.get("medical_note_pathway"):
        lines += ["", f"Medical documentation: {intake['medical_note_pathway']}"]
    lines.append("")
    lines += next_step_lines(intake)
    return lines


def next_step_lines(intake: Mapping[str, Any]) -> List[str]:
    state = intake.get("state")
    intake_id = intake.get("id")
    if state == "collecting":
        return [f"Next: tourclaim intake set {intake_id} <field>=<value> ..."]
    if state == "needs_approval":
        return [
            "Next: the traveler reviews and signs in their own browser. This tool cannot sign for them.",
            f"  Review link: {intake.get('review_url') or '(run tourclaim intake show to get it)'}",
            f"  Then: tourclaim intake sign {intake_id} --wait",
        ]
    if state == "ready_to_submit":
        return [f"Next: tourclaim intake submit {intake_id}"]
    if state == "submitted" and intake.get("claim_id"):
        claim_id = intake["claim_id"]
        return [f"Claim: {claim_id}  (tourclaim claims show {claim_id})"]
    return []


def claim_lines(claim: Mapping[str, Any], now: float) -> List[str]:
    lines = [f"Claim {claim.get('id')}"]

    def pad(label: str) -> str:
        return "  " + label.ljust(12) + " "

    updated = claim.get("updated_at")
    lines.append(pad("Status:") + str(claim.get("status")))
    lines.append(pad("Next:") + str(claim.get("next_action")))
    lines.append(pad("Updated:") + f"{format_time(updated)} ({relative_time(updated, now)})")
    lines.append(pad("Draft:") + str(claim.get("intake_id")))
    notice = mode_notice(claim.get("mode"))
    if notice:
        lines.append(pad("Mode:") + notice)
    return lines


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> List[str]:
    """A simple left-aligned table."""
    widths = [max([len(h)] + [len(r[i]) if i < len(r) else 0 for r in rows]) for i, h in enumerate(headers)]

    def fmt(cells: Sequence[str]) -> str:
        out = [c if i == len(cells) - 1 else c.ljust(widths[i]) for i, c in enumerate(cells)]
        return "  ".join(out).rstrip()

    return [fmt(headers)] + [fmt(r) for r in rows]


def card_lines(cards: Sequence[Dict[str, Any]]) -> List[str]:
    return table(["ID", "ISSUER", "CARD"], [[str(c.get("id")), str(c.get("issuer")), str(c.get("name"))] for c in cards])
