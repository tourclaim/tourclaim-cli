"""Intake fields: the schema's field table, and parsing of ``field=value`` pairs."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .errors import UsageError
from .models import OTHER_INSURANCE, REASON_CATEGORIES

# (kind, enum values). Kinds: string, date, decimal, integer, boolean, enum, object.
FieldKind = Tuple[str, Tuple[str, ...]]

_STRING: FieldKind = ("string", ())
_DATE: FieldKind = ("date", ())
_DECIMAL: FieldKind = ("decimal", ())
_INTEGER: FieldKind = ("integer", ())
_BOOLEAN: FieldKind = ("boolean", ())

#: IntakeFields-Input, in the schema's order.
INTAKE_FIELDS: Dict[str, FieldKind] = {
    "merchant_name": _STRING,
    "booking_ref": _STRING,
    "trip_date": _DATE,
    "booking_amount": _DECIMAL,
    "refunded_amount": _DECIMAL,
    "currency": ("enum", ("USD",)),
    "card_product_id": _INTEGER,
    "reason_category": ("enum", REASON_CATEGORIES),
    "narrative": _STRING,
    "cancellation_policy": _STRING,
    "other_insurance": ("enum", OTHER_INSURANCE),
    "other_insurance_details": _STRING,
    "medical": ("object", ()),
}

#: MedicalAnswers, in the schema's order. Set as medical.<name>.
MEDICAL_FIELDS: Dict[str, FieldKind] = {
    "symptom_onset_date": _DATE,
    "provider_seen": _BOOLEAN,
    "provider_details": _STRING,
    "prior_condition": _BOOLEAN,
    "prior_condition_details": _STRING,
    "stability_60day": _BOOLEAN,
    "stability_60day_details": _STRING,
}

Fields = Dict[str, Any]

_PAIR = re.compile(r"([A-Za-z0-9_.]+)(:?=)(.*)", re.S)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DECIMAL_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_INTEGER_RE = re.compile(r"[0-9]+")


def _unknown_field(key: str) -> UsageError:
    names = list(INTAKE_FIELDS) + [f"medical.{m}" for m in MEDICAL_FIELDS]
    return UsageError(f'Unknown field "{key}". Fields: {", ".join(names)}.')


def _kind_for(key: str) -> FieldKind:
    parts = key.split(".")
    if len(parts) > 2:
        raise _unknown_field(key)
    if len(parts) == 1:
        kind = INTAKE_FIELDS.get(parts[0])
    elif parts[0] == "medical":
        kind = MEDICAL_FIELDS.get(parts[1])
    else:
        kind = None
    if kind is None:
        raise _unknown_field(key)
    return kind


def _coerce(key: str, kind: FieldKind, raw: str) -> Any:
    name, values = kind
    if name == "string":
        return raw
    if name == "date":
        if not _DATE_RE.fullmatch(raw):
            raise UsageError(f"{key} must be a date like 2026-03-04.")
        return raw
    if name == "decimal":
        if not _DECIMAL_RE.fullmatch(raw):
            raise UsageError(f"{key} must be an amount like 250.00, with no currency symbol or commas.")
        return raw
    if name == "integer":
        if not _INTEGER_RE.fullmatch(raw):
            raise UsageError(f"{key} must be a whole number.")
        return int(raw)
    if name == "boolean":
        lowered = raw.lower()
        if lowered in ("true", "yes"):
            return True
        if lowered in ("false", "no"):
            return False
        raise UsageError(f"{key} must be true or false.")
    if name == "enum":
        upper = raw.upper()
        if upper not in values:
            raise UsageError(f"{key} must be one of: {', '.join(values)}.")
        return upper
    raise UsageError(f"Set {key} one answer at a time ({key}.<name>=value) or as JSON ({key}:='{{...}}').")


def _set_path(fields: Fields, key: str, value: Any) -> None:
    top, _, sub = key.partition(".")
    if not sub:
        fields[top] = value
        return
    current = fields.get(top)
    if top in fields and current is None:
        raise UsageError(f"{top} is being cleared and set in the same command.")
    obj = current if isinstance(current, dict) else {}
    obj[sub] = value
    fields[top] = obj


def _check_keys(obj: Fields, allowed: Dict[str, FieldKind], prefix: str) -> None:
    for key in obj:
        if key not in allowed:
            raise _unknown_field(prefix + key)


def parse_assignments(pairs: Iterable[str], into: Optional[Fields] = None) -> Fields:
    """Parses ``key=value`` (typed by the schema) and ``key:=<json>`` (raw JSON)
    pairs. Medical answers are addressed as ``medical.<name>``."""
    fields: Fields = {} if into is None else into
    for pair in pairs:
        match = _PAIR.fullmatch(pair)
        if not match:
            shown = pair[:40] + "..." if len(pair) > 40 else pair
            raise UsageError(f'Expected key=value or key:=<json>, got "{shown}".')
        key, op, raw = match.groups()
        kind = _kind_for(key)
        if op == ":=":
            try:
                value = json.loads(raw)
            except ValueError:
                raise UsageError(f'{key}:= needs a JSON value, such as 412, true, null or a quoted "string".') from None
            if kind[0] == "object" and value is not None and not isinstance(value, dict):
                raise UsageError(f"{key} must be a JSON object.")
            if kind[0] == "object" and isinstance(value, dict):
                _check_keys(value, MEDICAL_FIELDS, f"{key}.")
            _set_path(fields, key, value)
        else:
            if raw == "":
                raise UsageError(f"{key}= has no value. To clear a field use --clear {key}.")
            _set_path(fields, key, _coerce(key, kind, raw))
    return fields


def apply_clears(names: Iterable[str], into: Fields) -> Fields:
    """Sets each named field to ``None``, which the API treats as "clear"."""
    for name in names:
        _kind_for(name)
        top, _, sub = name.partition(".")
        if not sub:
            already = top in into
        else:
            current = into.get(top)
            already = isinstance(current, dict) and sub in current
        if already:
            raise UsageError(f"{name} is both set and cleared.")
        _set_path(into, name, None)
    return into


def read_fields_file(path: str, read_stdin: Callable[[], bytes]) -> Fields:
    """Reads a JSON object of fields from a file, or from stdin when path is ``-``."""
    try:
        if path == "-":
            text = read_stdin().decode("utf-8")
        else:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        reason = getattr(error, "strerror", None) or str(error)
        raise UsageError(f"Could not read {path}: {reason}") from None
    try:
        value = json.loads(text)
    except ValueError:
        raise UsageError(f"{path} is not valid JSON.") from None
    if not isinstance(value, dict):
        raise UsageError(f'{path} must hold a JSON object of intake fields, such as {{"merchant_name": "Example Air"}}.')
    _check_keys(value, INTAKE_FIELDS, "")
    medical = value.get("medical")
    if medical is not None:
        if not isinstance(medical, dict):
            raise UsageError("medical must be a JSON object.")
        _check_keys(medical, MEDICAL_FIELDS, "medical.")
    return value


def build_fields(
    *,
    file: Optional[str] = None,
    pairs: Iterable[str] = (),
    clears: Iterable[str] = (),
    read_stdin: Callable[[], bytes] = lambda: b"",
) -> Fields:
    """Builds a fields object from a file, then pairs, then clears."""
    fields: Fields = read_fields_file(file, read_stdin) if file else {}
    if isinstance(fields.get("medical"), dict):
        fields["medical"] = dict(fields["medical"])
    parse_assignments(pairs, fields)
    apply_clears(clears, fields)
    return fields


def field_names() -> List[str]:
    return list(INTAKE_FIELDS) + [f"medical.{m}" for m in MEDICAL_FIELDS]
