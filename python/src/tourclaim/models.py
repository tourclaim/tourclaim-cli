"""Shapes of the TourClaim connector API, as typed dictionaries.

Written by hand from the OpenAPI document served at
``/api/connectors/v1/openapi.json``. Responses are returned as plain ``dict``
objects; these types describe them for editors and type checkers. The device
authorization endpoints (``/api/connectors/device/*``) are not part of that
schema; their shapes follow RFC 8628.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

Mode = Literal["review", "live"]
IntakeState = Literal["collecting", "needs_approval", "ready_to_submit", "submitted"]
DocType = Literal["receipt", "itinerary", "medical_note", "other"]
EmailProvider = Literal["gmail", "outlook", "user"]
Scope = Literal["intakes:write", "evidence:write", "claims:submit", "claims:read"]

REASON_CATEGORIES = (
    "ILLNESS",
    "INJURY",
    "DEATH_IN_FAMILY",
    "MILITARY_DEPLOYMENT",
    "WEATHER",
    "AIRLINE_CANCELLATION",
    "OTHER",
)
OTHER_INSURANCE = ("YES", "NO", "UNSURE")
INTAKE_STATES = ("collecting", "needs_approval", "ready_to_submit", "submitted")
DOC_TYPES = ("receipt", "itinerary", "medical_note", "other")
ATTACHMENT_CONTENT_TYPES = ("application/pdf", "image/jpeg", "image/png")
EMAIL_PROVIDERS = ("gmail", "outlook", "user")
SCOPES = ("intakes:write", "evidence:write", "claims:submit", "claims:read")


class MedicalAnswers(TypedDict, total=False):
    """Every answer is optional; ``None`` clears it."""

    symptom_onset_date: Optional[str]
    provider_seen: Optional[bool]
    provider_details: Optional[str]
    prior_condition: Optional[bool]
    prior_condition_details: Optional[str]
    stability_60day: Optional[bool]
    stability_60day_details: Optional[str]


class IntakeFields(TypedDict, total=False):
    """Intake answers. Omitted means unchanged; ``None`` clears the field.
    Amounts are decimal strings such as ``"250.00"``."""

    merchant_name: Optional[str]
    booking_ref: Optional[str]
    trip_date: Optional[str]
    booking_amount: Optional[Union[str, float, int]]
    refunded_amount: Optional[Union[str, float, int]]
    currency: Optional[Literal["USD"]]
    card_product_id: Optional[int]
    reason_category: Optional[str]
    narrative: Optional[str]
    cancellation_policy: Optional[str]
    other_insurance: Optional[str]
    other_insurance_details: Optional[str]
    medical: Optional[MedicalAnswers]


class EvidenceSummary(TypedDict):
    id: str
    kind: str
    label: str


class _IntakeResponseRequired(TypedDict):
    id: str
    revision: int
    fields: IntakeFields
    missing_fields: List[str]
    next_questions: List[str]
    evidence: List[EvidenceSummary]
    state: IntakeState
    review_url: Optional[str]
    claim_id: Optional[str]
    medical_note_pathway: str


class IntakeResponse(_IntakeResponseRequired, total=False):
    """A claim draft (an "intake")."""

    mode: Mode
    evidence_upload_url: Optional[str]
    coverage_status: Literal["not_determined"]


class _ClaimResponseRequired(TypedDict):
    id: str
    intake_id: str
    status: str
    next_action: str
    updated_at: str


class ClaimResponse(_ClaimResponseRequired, total=False):
    """A submitted claim. Relay ``next_action`` as written."""

    mode: Mode


class _CardResponseRequired(TypedDict):
    id: int
    name: str
    issuer: str


class CardResponse(_CardResponseRequired, total=False):
    """A card product. Finding one does not mean it covers the loss."""

    coverage_status: Literal["requires_review"]


class KeyInfo(TypedDict):
    """``GET /api/connectors/v1/key``: the key making the request."""

    id: str
    expires_at: str
    scopes: List[str]
    account_email: str


class _ConnectorInfoRequired(TypedDict):
    name: str
    version: str
    enabled: bool
    mode: Mode
    openapi_url: str
    connection_url: str
    documentation_url: str


class ConnectorInfo(_ConnectorInfoRequired, total=False):
    """``GET /api/connectors/v1``. URLs are absolute (older servers sent paths)."""

    cli_login_url: str


class _DeviceCodeRequired(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int


class DeviceCode(_DeviceCodeRequired, total=False):
    """``POST /api/connectors/device/code``. ``device_code`` is a secret: never show it."""

    verification_uri_complete: Optional[str]
    interval: int


class DeviceToken(TypedDict):
    """``POST /api/connectors/device/token`` once approved. ``api_key`` is a secret."""

    api_key: str
    expires_at: str
    scopes: List[str]
    grant_id: str


JSONObject = Dict[str, Any]
