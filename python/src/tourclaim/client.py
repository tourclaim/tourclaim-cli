"""A client for TourClaim's traveler connector API.

One :class:`Client` acts for one traveler, with a key that belongs to them.
It has one method per API operation, plus helpers for signing in with a device
code (:meth:`Client.device_login`) and waiting for the traveler's signature
(:meth:`Client.wait_for_signature`). The client can never sign: the traveler
reviews and signs the authorization in their own browser at ``review_url``.

Errors are raised as subclasses of :class:`tourclaim.errors.TourClaimError`,
each carrying ``status``, ``code`` and ``message``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlencode

from .config import is_http_url, normalize_api_url, resolve_api_url
from .config import user_agent as default_user_agent
from .credentials import CredentialStore, StoredCredential
from .errors import (
    AccessDeniedError,
    APIError,
    AuthenticationError,
    AuthorizationPendingError,
    ConflictError,
    ConsentRequiredError,
    DeviceFlowError,
    ExpiredTokenError,
    FileTooLargeError,
    NetworkError,
    NotFoundError,
    NotSignedInError,
    PayloadTooLargeError,
    PermissionDeniedError,
    RateLimitError,
    RequestTimeoutError,
    SignatureTimeoutError,
    SlowDownError,
    TourClaimError,
    UnavailableError,
    UnexpectedResponseError,
    UnsupportedFileError,
    UsageError,
    ValidationError,
)
from .filetype import MAX_ATTACHMENT_BYTES, detect_content_type
from .format import iso_utc, parse_iso
from .models import (
    DOC_TYPES,
    EMAIL_PROVIDERS,
    SCOPES,
    CardResponse,
    ClaimResponse,
    ConnectorInfo,
    DeviceCode,
    DeviceToken,
    IntakeFields,
    IntakeResponse,
    KeyInfo,
)

API_PREFIX = "/api/connectors/v1"
DEVICE_CODE_PATH = "/api/connectors/device/code"
DEVICE_TOKEN_PATH = "/api/connectors/device/token"

KEY_FORMAT = re.compile(r"tc_muse_[A-Za-z0-9_-]+")
IDEMPOTENCY_KEY_FORMAT = re.compile(r"[A-Za-z0-9_-]{8,100}")
FILENAME_FORMAT = re.compile(r"[^/\\\x00-\x1f]+")
SENT_AT_FORMAT = re.compile(r".*T.*(Z|[+-][0-9]{2}:?[0-9]{2})", re.I | re.S)
MAX_EMAIL_TEXT = 30_000
MAX_RETRY_AFTER_SECONDS = 60
DEFAULT_RETRY_AFTER_SECONDS = 5
DEFAULT_TIMEOUT = 90.0
MAX_POLL_FAILURES = 3

# For servers that predate X-TourClaim-Error: the same causes, read from the message.
_CONFLICT_MESSAGES = (
    (re.compile(r"already used with different fields", re.I), "idempotency_key_reused"),
    (re.compile(r"earlier connection", re.I), "idempotency_key_other_connection"),
    (re.compile(r"concurrent request", re.I), "concurrent_request"),
    (re.compile(r"read-only|submitted claim cannot be deleted", re.I), "intake_submitted"),
    (re.compile(r"already imported with different contents", re.I), "evidence_conflict"),
    (re.compile(r"incomplete|remaining questions", re.I), "intake_incomplete"),
    (re.compile(r"authorization changed|approval expired", re.I), "approval_outdated"),
    (re.compile(r"must approve", re.I), "approval_required"),
    (re.compile(r"already exists", re.I), "duplicate_booking"),
    (re.compile(r"intake changed", re.I), "stale_revision"),
)


class Response:
    """An API response: status, parsed JSON body and lower-case headers."""

    __slots__ = ("status", "data", "headers")

    def __init__(self, status: int, data: Any, headers: Mapping[str, str]) -> None:
        self.status = status
        self.data = data
        self.headers = dict(headers)


# ---- error helpers ----


def sentences(*parts: Optional[str]) -> str:
    """Joins sentences, adding a full stop where one is missing."""
    out = []
    for part in parts:
        text = (part or "").strip()
        if not text:
            continue
        out.append(text if re.search(r"[.!?:)]$", text) else f"{text}.")
    return " ".join(out)


#: Every operation, including the drafts list and the key endpoints the CLI uses.
CLI_SCHEMA_PATH = f"{API_PREFIX}/openapi-cli.json"
#: The ten operations assistants load as tools; servers before 1.57.1 serve everything here.
ASSISTANT_SCHEMA_PATH = f"{API_PREFIX}/openapi.json"

#: Statuses that mean "try again shortly": a gateway timeout or error at the
#: edge, or the server's 503 for a brief database lock clash.
TRANSIENT_STATUSES = (502, 503, 504)


def is_transient_status(error: BaseException) -> bool:
    """A server answer worth retrying a few times in a polling loop."""
    return isinstance(error, APIError) and error.status in TRANSIENT_STATUSES


def transient_wait_seconds(error: APIError, cap: float) -> float:
    """Seconds to wait after a transient answer: its Retry-After, capped at ``cap``, else ``cap``."""
    header = error.headers.get("retry-after")
    return float(min(retry_after_seconds(header), cap)) if header is not None else float(cap)


def _retry_after_extra(headers: Mapping[str, str]) -> Dict[str, Any]:
    value = headers.get("retry-after")
    return {} if value is None else {"retry_after": retry_after_seconds(value)}


def retry_after_seconds(value: Optional[str], now: Optional[float] = None) -> int:
    """Seconds from a Retry-After header (delta-seconds or an HTTP date)."""
    if not value:
        return DEFAULT_RETRY_AFTER_SECONDS
    text = value.strip()
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return DEFAULT_RETRY_AFTER_SECONDS
    if when is None:
        return DEFAULT_RETRY_AFTER_SECONDS
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = time.time() if now is None else now
    return max(0, int(-(-(when.timestamp() - current) // 1)))


def issue_location(issue: Mapping[str, Any]) -> str:
    """Readable text for a validation issue's location, such as fields.trip_date."""
    loc = issue.get("loc")
    parts = [str(p) for p in loc] if isinstance(loc, list) else []
    if parts and parts[0] in ("body", "query", "path"):
        parts = parts[1:]
    return ".".join(parts) or "request"


def describe_error_body(status: int, body: Any) -> str:
    """A sentence describing an error body of any of the API's shapes."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, list):
            issues = []
            for issue in detail:
                issue = issue if isinstance(issue, dict) else {}
                where = issue_location(issue)
                issues.append(f"{where} ({issue['type']})" if issue.get("type") else where)
            if issues:
                return f"The API rejected these values: {', '.join(issues)}."
            return "The API rejected the request as invalid."
        description = body.get("error_description")
        if isinstance(description, str) and description:
            return description
        error = body.get("error")
        if isinstance(error, str) and error:
            return error
    return f"The API answered HTTP {status}."


def conflict_code(headers: Optional[Mapping[str, str]], body: Any) -> str:
    """The cause of a 409: the X-TourClaim-Error header, else the message, else ``conflict``."""
    header = ((headers or {}).get("x-tourclaim-error") or "").strip()
    if re.fullmatch(r"[a-z0-9_]{1,64}", header):
        return header
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        for pattern, code in _CONFLICT_MESSAGES:
            if pattern.search(detail):
                return code
    return "conflict"


def to_api_error(status: int, body: Any, headers: Mapping[str, str]) -> APIError:
    """Maps an HTTP error response to the matching exception."""
    message = describe_error_body(status, body)
    data = None if isinstance(body, str) else body
    if 300 <= status < 400:
        location = headers.get("location")
        where = f" to {location}" if location else ""
        return APIError(status, f"The API redirected{where}. Check --api-url.", code="redirect", body=data, headers=headers)
    if status == 401:
        return AuthenticationError(
            status, sentences(message, "The key is missing, expired or revoked. Run: tourclaim login"), body=data, headers=headers
        )
    if status == 403:
        return PermissionDeniedError(
            status,
            sentences(
                message,
                "This key was not granted permission for this action. Run tourclaim login --force to connect again with the permission it needs",
            ),
            body=data,
            headers=headers,
        )
    if status == 404:
        return NotFoundError(status, message, body=data, headers=headers)
    if status == 409:
        return ConflictError(status, message, code=conflict_code(headers, data), body=data, headers=headers)
    if status == 413:
        return PayloadTooLargeError(
            status, sentences(message, "The request was too large; files may be at most 5 MiB"), body=data, headers=headers
        )
    if status == 422:
        return ValidationError(status, message, body=data, headers=headers)
    if status == 429:
        wait = retry_after_seconds(headers.get("retry-after"))
        return RateLimitError(
            status, sentences(message, f"Try again in {wait} seconds"), body=data, headers=headers, extra={"retry_after": wait}
        )
    if status == 503:
        return UnavailableError(
            status,
            sentences(message, "The TourClaim connector is unavailable right now; nothing was changed. Try again later"),
            body=data,
            headers=headers,
            extra=_retry_after_extra(headers),
        )
    extra = _retry_after_extra(headers) if status in TRANSIENT_STATUSES else None
    return APIError(status, message, body=data, headers=headers, extra=extra)


def derive_message_id(sender: str, subject: str, sent_at: Optional[str], text: str) -> str:
    """A stable id for an email without one, so adding it twice changes nothing.
    The same message gives the same id in the Node edition."""
    digest = hashlib.sha256(f"{sender}\n{subject}\n{sent_at or ''}\n{text}".encode("utf-8")).hexdigest()
    return f"sha256-{digest[:40]}"


def normalize_sent_at(value: Any) -> Optional[str]:
    """An ISO 8601 date and time with a zone (or an aware datetime), as UTC
    ``2026-03-03T14:05:00.000Z``. Raises UsageError otherwise."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise UsageError("sent_at must include a time zone.")
        return iso_utc(value)
    moment = parse_iso(value, default_utc=False) if isinstance(value, str) and SENT_AT_FORMAT.fullmatch(value) else None
    if moment is None:
        raise UsageError("sent_at must be an ISO 8601 date and time with a zone, such as 2026-03-03T14:05:00Z.")
    return iso_utc(moment)


def _intake_path(intake_id: str, suffix: str = "") -> str:
    return f"{API_PREFIX}/intakes/{quote(str(intake_id), safe='')}{suffix}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed: a key is only ever sent to the configured URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class Client:
    """A client for one traveler.

    ``api_key``: the traveler's key (``tc_muse_...``). When omitted, the client
    uses ``TOURCLAIM_API_KEY``, then the key ``tourclaim login`` stored for this
    API URL (pass ``load_credentials=False`` to use neither).

    ``api_url``: the API base URL. When omitted, ``TOURCLAIM_API_URL``, then
    ``https://app.getcopernican.com``. Plain ``http`` is allowed only for localhost.

    Keyword options: ``timeout`` (seconds per request, default 90),
    ``retry_rate_limit`` (retry once after a 429 asking for at most 60 seconds,
    default True), ``user_agent``, ``sleep`` (used for waits) and ``on_notice``
    (called with progress messages such as a rate-limit wait).

    After each response, ``mode`` holds the API's ``X-TourClaim-Mode``:
    ``review`` (synthetic claims, nothing filed) or ``live``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        *,
        load_credentials: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
        retry_rate_limit: bool = True,
        user_agent: Optional[str] = None,
        sleep: Optional[Callable[[float], None]] = None,
        on_notice: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.api_url = normalize_api_url(api_url, "api_url") if api_url is not None else resolve_api_url(None, os.environ)
        self._api_key: Optional[str] = api_key or None
        self._key_resolved = bool(api_key) or not load_credentials
        #: Where the key came from: "argument", "env", "stored", "device_login" or None.
        self.key_source: Optional[str] = "argument" if api_key else None
        self.timeout = timeout
        self.retry_rate_limit = retry_rate_limit
        self.user_agent = user_agent or default_user_agent()
        self._sleep = sleep or time.sleep
        self._notice = on_notice or (lambda message: None)
        #: The X-TourClaim-Mode of the last response: "review", "live" or None.
        self.mode: Optional[str] = None

    def __repr__(self) -> str:
        return f"Client(api_url={self.api_url!r}, key={'set' if self.has_api_key else 'none'})"

    # ---- keys ----

    def _key(self) -> Optional[str]:
        if not self._key_resolved:
            self._key_resolved = True
            from_env = os.environ.get("TOURCLAIM_API_KEY", "").strip()
            if from_env:
                self._api_key, self.key_source = from_env, "env"
            else:
                stored = CredentialStore.default().get(self.api_url)
                if stored:
                    self._api_key, self.key_source = stored.api_key, "stored"
        return self._api_key

    @property
    def has_api_key(self) -> bool:
        """Whether the client has a key to send. The key itself is never exposed."""
        return self._key() is not None

    # ---- transport ----

    def _send(self, method: str, url: str, headers: Dict[str, str], data: Optional[bytes]):  # type: ignore[no-untyped-def]
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        handlers: List[Any] = [_NoRedirect()]
        if url.startswith("http://"):
            # Plain http is only allowed to localhost; never route it through a proxy.
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            try:
                body = error.read()
            except Exception:  # noqa: BLE001 - an unreadable error body is still an error
                body = b""
            finally:
                error.close()
            return error.code, body, error.headers

    def _transport_error(self, error: BaseException) -> TourClaimError:
        reason: Any = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return RequestTimeoutError(f"The request to {self.api_url} timed out.")
        if isinstance(reason, ssl.SSLCertVerificationError):
            return NetworkError(
                f"Could not verify the TLS certificate of {self.api_url} ({reason.verify_message or reason}). "
                "If Python was installed from python.org on macOS, run its Install Certificates command."
            )
        text = getattr(reason, "strerror", None) or str(reason) or type(reason).__name__
        return NetworkError(f"Could not reach {self.api_url} ({text}).")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, Any]] = None,
        auth: bool = True,
        allow_status: Sequence[int] = (),
        retry_rate_limit: Optional[bool] = None,
    ) -> Response:
        """Sends one request to ``api_url + path`` and returns the parsed response.

        Raises the matching :class:`APIError` subclass for an error status
        (except those in ``allow_status``), after one automatic retry for a 429
        that asks for at most 60 seconds.
        """
        send_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        send_headers.update(headers or {})
        if auth:
            key = self._key()
            if not key:
                raise NotSignedInError(f"Not signed in to {self.api_url}. Run: tourclaim login")
            send_headers["Authorization"] = f"Bearer {key}"
        data = None
        if body is not None:
            send_headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = self.api_url + path
        params = {k: v for k, v in (query or {}).items() if v is not None}
        if params:
            url += "?" + urlencode({k: str(v) for k, v in params.items()})
        retry = self.retry_rate_limit if retry_rate_limit is None else retry_rate_limit

        attempt = 0
        while True:
            try:
                status, raw, raw_headers = self._send(method, url, send_headers, data)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, http.client.HTTPException, OSError) as error:
                raise self._transport_error(error) from None
            response_headers = {k.lower(): v for k, v in (raw_headers.items() if raw_headers else [])}
            parsed = _parse_body(raw)
            mode = response_headers.get("x-tourclaim-mode")
            if mode:
                self.mode = mode
            allowed = status in allow_status
            if status == 429 and not allowed and attempt == 0 and retry:
                wait = retry_after_seconds(response_headers.get("retry-after"))
                if wait <= MAX_RETRY_AFTER_SECONDS:
                    self._notice(f"Rate limited by the API; retrying in {wait} second{'' if wait == 1 else 's'}.")
                    self._sleep(wait)
                    attempt += 1
                    continue
            if 200 <= status < 300 or allowed:
                return Response(status, parsed, response_headers)
            raise to_api_error(status, parsed, response_headers)

    # ---- discovery ----

    def connector_info(self) -> ConnectorInfo:
        """``GET /api/connectors/v1``: name, version, ``enabled``, ``mode`` and links. No key needed."""
        return self.request("GET", API_PREFIX, auth=False).data

    def schema(self) -> Dict[str, Any]:
        """The live OpenAPI document with every operation, as the CLI uses them
        (``/api/connectors/v1/openapi-cli.json``). A server that does not have it
        yet (before 1.57.1) answers 404, and then ``/openapi.json`` is returned
        instead. ``/openapi.json`` itself lists only the ten operations assistants
        load as tools. No key needed."""
        try:
            return self.request("GET", CLI_SCHEMA_PATH, auth=False).data
        except NotFoundError:
            return self.request("GET", ASSISTANT_SCHEMA_PATH, auth=False).data

    # ---- the key ----

    def get_connection(self) -> KeyInfo:
        """``get_connection``: the key's id, expiry and scopes. ``account_email`` is
        set only for keys from ``tourclaim login`` (the device flow); it is null for
        keys made at /connect/muse."""
        return self.request("GET", f"{API_PREFIX}/key").data

    def disconnect(self) -> None:
        """``disconnect``: revokes the key making the request, at once."""
        self.request("DELETE", f"{API_PREFIX}/key")

    # ---- cards ----

    def search_cards(self, query: str) -> List[CardResponse]:
        """``search_credit_cards``: card products by name (never a card number), at most 30.
        Use a result's ``id`` as ``card_product_id``. A match does not mean the card covers the loss."""
        return self.request("GET", f"{API_PREFIX}/cards", query={"q": query}).data

    # ---- intakes (claim drafts) ----

    def start_intake(self, fields: Optional[IntakeFields] = None, *, idempotency_key: Optional[str] = None) -> IntakeResponse:
        """``start_travel_claim``: starts a draft. Every field is optional here.

        A random ``Idempotency-Key`` is sent unless one is given. To retry a
        failed or timed-out call without creating a second draft, pass the
        same fields and the same key; an error carries the key it used in
        ``error.extra["idempotency_key"]``.
        """
        key = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        if not IDEMPOTENCY_KEY_FORMAT.fullmatch(key):
            raise UsageError("idempotency_key must be 8 to 100 letters, digits, - or _.")
        try:
            return self.request(
                "POST", f"{API_PREFIX}/intakes", body={"fields": dict(fields or {})}, headers={"Idempotency-Key": key}
            ).data
        except TourClaimError as error:
            error.extra["idempotency_key"] = key
            raise

    def list_drafts(self, offset: int = 0) -> List[IntakeResponse]:
        """``list_claim_drafts``: drafts never submitted, most recently changed first, 30 at a time."""
        return self.request("GET", f"{API_PREFIX}/intakes", query={"offset": offset}).data

    def get_intake(self, intake_id: str) -> IntakeResponse:
        """``get_travel_claim_intake``: a draft, what it still needs and its state."""
        return self.request("GET", _intake_path(intake_id)).data

    def update_intake(self, intake_id: str, fields: IntakeFields, *, expected_revision: int) -> IntakeResponse:
        """``update_travel_claim_intake``: saves only the given fields (``None`` clears one).

        Raises :class:`ConflictError` with code ``stale_revision`` when the
        draft is no longer at ``expected_revision``. Any change after the
        traveler signed cancels the signature.
        """
        body = {"expected_revision": expected_revision, "fields": dict(fields)}
        return self.request("PATCH", _intake_path(intake_id), body=body).data

    def delete_draft(self, intake_id: str) -> None:
        """``delete_travel_claim_draft``: permanently deletes a draft that was never submitted."""
        self.request("DELETE", _intake_path(intake_id))

    def add_email(
        self,
        intake_id: str,
        *,
        expected_revision: int,
        subject: str,
        sender: str,
        text: str,
        user_authorized_sharing: bool,
        message_id: Optional[str] = None,
        sent_at: Any = None,
        provider: str = "user",
    ) -> IntakeResponse:
        """``import_selected_email``: saves one email the traveler chose to share, unmodified.

        ``user_authorized_sharing`` must be ``True``, and only after the
        traveler agreed to share this message; there is no default. Without
        ``message_id`` a stable one is derived, so adding the same message
        twice changes nothing.
        """
        if user_authorized_sharing is not True:
            raise ConsentRequiredError(
                "Evidence is shared only with the traveler's agreement: pass user_authorized_sharing=True once they agreed to share this message."
            )
        if provider not in EMAIL_PROVIDERS:
            raise UsageError(f"provider must be one of: {', '.join(EMAIL_PROVIDERS)}.")
        sent = normalize_sent_at(sent_at)
        if not isinstance(text, str) or not text.strip():
            raise UsageError("The message text is empty.")
        if len(text) > MAX_EMAIL_TEXT:
            raise UsageError(f"The message text is {len(text):,} characters; the API accepts at most 30,000.")
        if len(subject) > 500:
            raise UsageError("The subject is longer than 500 characters.")
        if not sender or len(sender) > 320:
            raise UsageError("The sender must be 1 to 320 characters.")
        if message_id is None:
            message_id = derive_message_id(sender, subject, sent, text)
        if not message_id or len(message_id) > 255:
            raise UsageError("message_id must be 1 to 255 characters.")
        body = {
            "expected_revision": expected_revision,
            "provider": provider,
            "message_id": message_id,
            "subject": subject,
            "sender": sender,
            "sent_at": sent,
            "text": text,
            "user_authorized_sharing": True,
        }
        return self.request("POST", _intake_path(intake_id, "/email-evidence"), body=body).data

    def add_attachment(
        self,
        intake_id: str,
        *,
        expected_revision: int,
        filename: str,
        content: bytes,
        doc_type: str,
        user_authorized_sharing: bool,
        content_type: Optional[str] = None,
    ) -> IntakeResponse:
        """``import_claim_attachment``: uploads one PDF, JPEG or PNG of at most 5 MiB.

        The type is detected from the first bytes. ``doc_type`` is one of
        ``receipt``, ``itinerary``, ``medical_note`` or ``other``.
        ``user_authorized_sharing`` must be ``True``, and only after the
        traveler agreed to share this file; there is no default.
        """
        if user_authorized_sharing is not True:
            raise ConsentRequiredError(
                "Evidence is shared only with the traveler's agreement: pass user_authorized_sharing=True once they agreed to share this file."
            )
        if doc_type not in DOC_TYPES:
            raise UsageError(f"doc_type must be one of: {', '.join(DOC_TYPES)}.")
        if not FILENAME_FORMAT.fullmatch(filename or "") or len(filename) > 200:
            raise UsageError("The file name must be 1 to 200 characters with no slashes or control characters.")
        if not content:
            raise UsageError("The file is empty.")
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise FileTooLargeError("The file is larger than 5 MiB, the most the API accepts.")
        detected = detect_content_type(content)
        if detected is None:
            raise UnsupportedFileError("The file is not a PDF, JPEG or PNG (checked by its contents, not its name).")
        if content_type is not None and content_type != detected:
            raise UnsupportedFileError(f"The file's contents are {detected}, not {content_type}.")
        body = {
            "expected_revision": expected_revision,
            "filename": filename,
            "content_type": detected,
            "doc_type": doc_type,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "user_authorized_sharing": True,
        }
        return self.request("POST", _intake_path(intake_id, "/attachments"), body=body).data

    def submit(self, intake_id: str, *, expected_revision: int) -> ClaimResponse:
        """``submit_authorized_travel_claim``: hands a signed draft to Copernican as a claim.

        Safe to retry: a repeated call returns the same claim. Raises
        :class:`ConflictError` (``approval_required``, ``approval_outdated``,
        ``intake_incomplete``, ``duplicate_booking``, ...) when it cannot.
        Submitting files nothing with an insurer and promises no reimbursement.
        """
        body = {"expected_revision": expected_revision}
        return self.request("POST", _intake_path(intake_id, "/submit"), body=body).data

    def wait_for_signature(
        self,
        intake_id: str,
        *,
        timeout: float = 900.0,
        interval: float = 5.0,
        sleep: Optional[Callable[[float], None]] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> IntakeResponse:
        """Waits until the traveler has signed (state ``ready_to_submit``) and
        returns the draft. Only the traveler can sign, in their own browser at
        the draft's ``review_url``; this only checks every ``interval`` seconds.

        Raises :class:`SignatureTimeoutError` after ``timeout`` seconds, and
        :class:`ConflictError` (``intake_incomplete``) if the draft needs answers.
        """
        sleep = sleep or self._sleep
        now = now or time.time
        deadline = now() + timeout
        current = self.get_intake(intake_id)
        failures = 0
        while True:
            state = current.get("state")
            if state in ("ready_to_submit", "submitted"):
                return current
            if state == "collecting":
                missing = current.get("missing_fields") or []
                raise ConflictError(
                    409,
                    f"Draft {intake_id} needs answers before it can be signed: {', '.join(missing)}.",
                    code="intake_incomplete",
                    extra={"state": state, "current_revision": current.get("revision"), "missing_fields": missing},
                )
            if now() + interval > deadline:
                raise SignatureTimeoutError(
                    f"The traveler has not signed draft {intake_id} after {timeout:g} seconds. The review link stays valid: {current.get('review_url')}",
                    extra={"review_url": current.get("review_url")},
                )
            pause = interval
            while True:
                sleep(pause)
                try:
                    current = self.get_intake(intake_id)
                    failures = 0
                    break
                except (NetworkError, RequestTimeoutError, APIError) as error:
                    # No answer, or a temporary server error (502, 503, 504): keep waiting a few times.
                    if isinstance(error, APIError) and not is_transient_status(error):
                        raise
                    failures += 1
                    pause = transient_wait_seconds(error, interval) if isinstance(error, APIError) else interval
                    if failures >= MAX_POLL_FAILURES or now() + pause > deadline:
                        raise

    # ---- claims ----

    def list_claims(self, offset: int = 0) -> List[ClaimResponse]:
        """``list_my_claims``: submitted claims, newest first, 30 at a time."""
        return self.request("GET", f"{API_PREFIX}/claims", query={"offset": offset}).data

    def get_claim(self, claim_id: str) -> ClaimResponse:
        """``get_my_claim_status``: where a claim stands. Relay ``next_action`` as written."""
        return self.request("GET", f"{API_PREFIX}/claims/{quote(str(claim_id), safe='')}").data

    # ---- signing in with a device code (RFC 8628) ----

    def request_device_code(self, scopes: Optional[Sequence[str]] = None, *, client_name: Optional[str] = None) -> DeviceCode:
        """Starts a sign-in. Show the traveler ``verification_uri`` and ``user_code``:
        they open the page and type the code. Never show ``device_code``, and never
        put the code in a link."""
        body: Dict[str, Any] = {"client": client_name or self.user_agent}
        if scopes:
            for scope in scopes:
                if scope not in SCOPES:
                    raise UsageError(f'Unknown scope "{scope}". Scopes: {", ".join(SCOPES)}.')
            body["scopes"] = list(dict.fromkeys(scopes))
        code = self.request("POST", DEVICE_CODE_PATH, body=body, auth=False).data
        if (
            not isinstance(code, dict)
            or not isinstance(code.get("device_code"), str)
            or not isinstance(code.get("user_code"), str)
            or not is_http_url(code.get("verification_uri"))
            or not isinstance(code.get("expires_in"), (int, float))
            or isinstance(code.get("expires_in"), bool)
        ):
            raise UnexpectedResponseError("The API returned an unexpected sign-in response.")
        interval = code.get("interval")
        valid_interval = isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval >= 1
        return {
            "device_code": code["device_code"],
            "user_code": code["user_code"],
            "verification_uri": code["verification_uri"],
            # Older servers also sent verification_uri_complete; it is ignored.
            "expires_in": code["expires_in"],
            "interval": interval if valid_interval else 5,
        }

    def poll_device_token(self, device_code: str, replacing: Optional[str] = None) -> DeviceToken:
        """Polls once. Returns the key once the traveler approved; raises
        :class:`AuthorizationPendingError` or :class:`SlowDownError` while
        waiting, :class:`AccessDeniedError` or :class:`ExpiredTokenError` when it is over.

        ``replacing`` is a key this sign-in replaces (the one stored for this API
        URL). It is sent as a bearer so the server retires it when it mints the
        new key; the client never needs to revoke it afterwards."""
        return self._poll_device_token(device_code, [replacing])

    def _poll_device_token(self, device_code: str, bearer: List[Optional[str]]) -> DeviceToken:
        def send(key: Optional[str]) -> Any:
            return self.request(
                "POST",
                DEVICE_TOKEN_PATH,
                body={"device_code": device_code},
                headers={"Authorization": f"Bearer {key}"} if key else None,
                auth=False,
                allow_status=(400, 401, 429),
                retry_rate_limit=False,
            )

        response = send(bearer[0])
        if response.status == 401 and bearer[0]:
            # The key being replaced was not accepted; sign in without it from now on.
            bearer[0] = None
            response = send(None)
        if 200 <= response.status < 300:
            token = response.data if isinstance(response.data, dict) else {}
            api_key = token.get("api_key")
            if not isinstance(api_key, str) or not KEY_FORMAT.fullmatch(api_key):
                raise UnexpectedResponseError("The API returned a key in an unexpected format. Nothing was saved.")
            scopes = token.get("scopes")
            return {
                "api_key": api_key,
                "expires_at": token["expires_at"] if isinstance(token.get("expires_at"), str) else None,  # type: ignore[typeddict-item]
                "scopes": [s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else [],
                "grant_id": token["grant_id"] if isinstance(token.get("grant_id"), str) else None,  # type: ignore[typeddict-item]
            }
        if response.status == 429:
            raise SlowDownError("Polling too fast; slow down.", status=429)
        err = response.data if isinstance(response.data, dict) else {}
        error = err.get("error")
        description = err.get("error_description") if isinstance(err.get("error_description"), str) else None
        if error == "authorization_pending":
            raise AuthorizationPendingError("The traveler has not approved the sign-in yet.", status=400)
        if error == "slow_down":
            raise SlowDownError("Polling too fast; slow down.", status=400)
        if error == "access_denied":
            raise AccessDeniedError(sentences(description or "The sign-in was declined", "Nothing was saved."), status=400)
        if error == "expired_token":
            raise ExpiredTokenError("The sign-in code expired or was already used. Run tourclaim login again.", status=400)
        shown = error if isinstance(error, str) and error else f"HTTP {response.status}"
        raise DeviceFlowError(sentences(f"The sign-in failed ({shown})", description), status=response.status)

    def wait_for_device_token(
        self,
        code: DeviceCode,
        *,
        sleep: Optional[Callable[[float], None]] = None,
        now: Optional[Callable[[], float]] = None,
        replacing: Optional[str] = None,
    ) -> DeviceToken:
        """Polls at the interval the API asks for until the traveler approves,
        slowing down when told to. It never sleeps past the code's expiry, and the
        poll made at or after expiry is the last one: the server keeps an approved
        code alive a little longer, so an approval in the final seconds still
        yields a key. ``replacing`` is sent with each poll (see
        :meth:`poll_device_token`). The client then uses the new key."""
        sleep = sleep or self._sleep
        now = now or time.time
        interval = float(code.get("interval") or 5)
        interval = max(1.0, interval)
        deadline = now() + float(code["expires_in"])
        failures = 0
        next_wait: Optional[float] = None
        bearer: List[Optional[str]] = [replacing]

        def expired() -> ExpiredTokenError:
            return ExpiredTokenError("The sign-in code expired before it was approved. Run tourclaim login again.")

        while True:
            wait = next_wait if next_wait is not None else interval
            next_wait = None
            sleep(max(0.0, min(wait, deadline - now())))
            final = now() >= deadline
            try:
                token = self._poll_device_token(code["device_code"], bearer)
            except AuthorizationPendingError:
                if final:
                    raise expired() from None
                failures = 0
                continue
            except SlowDownError:
                if final:
                    raise expired() from None
                failures = 0
                interval += 5
                continue
            except (NetworkError, RequestTimeoutError):
                failures += 1
                if not final and failures < MAX_POLL_FAILURES:
                    interval = min(interval * 2, 60.0)
                    continue
                raise
            except APIError as error:
                # A temporary server error (502, 503, 504): the traveler may already
                # have approved, so keep going a few times rather than give up. The
                # poll at or after the deadline is still the last one.
                if not is_transient_status(error):
                    raise
                failures += 1
                if not final and failures < MAX_POLL_FAILURES:
                    next_wait = transient_wait_seconds(error, interval)
                    continue
                raise
            self._api_key, self._key_resolved, self.key_source = token["api_key"], True, "device_login"
            return token

    def device_login(
        self,
        scopes: Optional[Sequence[str]] = None,
        *,
        on_code: Optional[Callable[[DeviceCode], None]] = None,
        save: bool = False,
        client_name: Optional[str] = None,
        sleep: Optional[Callable[[float], None]] = None,
        now: Optional[Callable[[], float]] = None,
        replacing: Optional[str] = None,
    ) -> DeviceToken:
        """Signs in with a device code: the traveler opens the page and types the
        code in their own browser.

        ``on_code`` is called with the page and code to show the traveler; by
        default they are printed to stderr. Returns the new key's details, and the
        client uses the key from then on. With ``save=True`` the key is also stored
        where ``tourclaim login`` keeps it, and the key stored there before is sent
        as ``replacing`` so the server retires it. Pass ``replacing`` to name the
        key being replaced yourself.
        """
        if save and replacing is None:
            stored = CredentialStore.default().get(self.api_url)
            replacing = stored.api_key if stored else None
        code = self.request_device_code(scopes, client_name=client_name)
        if on_code is not None:
            on_code(code)
        else:
            sys.stderr.write(
                f"To connect to your TourClaim account, open {code['verification_uri']}\n"
                f"Enter this code on that page: {code['user_code']}\n"
            )
            sys.stderr.flush()
        token = self.wait_for_device_token(code, sleep=sleep, now=now, replacing=replacing)
        if save:
            CredentialStore.default().set(
                self.api_url,
                StoredCredential(token["api_key"], token.get("expires_at"), token.get("grant_id"), list(token.get("scopes") or [])),
            )
        return token
