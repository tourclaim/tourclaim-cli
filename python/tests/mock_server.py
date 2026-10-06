"""An in-process stand-in for the TourClaim connector API (stdlib http.server).

It implements the contract the client is written against: discovery, OpenAPI,
device authorization (pending, slow_down, denied, expired), key info and
revocation, cards, intakes with revisions and 409 causes in X-TourClaim-Error,
the drafts list (CLI keys of one traveler share drafts), evidence, signing
(simulated), submission, claims, and the API's error shapes (422 lists and
strings, 401 with WWW-Authenticate, 403, 404, 409, 413, 429 with Retry-After, 503).

The traveler's browser is simulated by /connect/cli, a page with a form where
the code is typed (submitting it to /connect/cli?code=... approves the sign-in),
and /connect/muse?intake=... (signs the current revision).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OPENAPI_PATH = os.path.join(ROOT, "openapi", "connectors-v1.json")
ALL_SCOPES = ["intakes:write", "evidence:write", "claims:submit", "claims:read"]
MAX_BODY = 8 * 1024 * 1024
REASONS = ["ILLNESS", "INJURY", "DEATH_IN_FAMILY", "MILITARY_DEPLOYMENT", "WEATHER", "AIRLINE_CANCELLATION", "OTHER"]
DECIMAL = re.compile(r"(?!^[-+.]*$)[+-]?0*\d*\.?\d{0,2}0*")
MAGIC = {
    "application/pdf": b"%PDF-",
    "image/jpeg": b"\xff\xd8\xff",
    "image/png": b"\x89PNG\r\n\x1a\n",
}
REQUIRED_FIELDS = {
    "merchant_name": "Who did you book with?",
    "booking_ref": "What is the booking confirmation number?",
    "trip_date": "What date was the trip or activity scheduled for?",
    "booking_amount": "How much did you pay for this booking?",
    "refunded_amount": "How much has already been refunded, including zero if nothing?",
    "currency": "Was the booking paid in US dollars? This pilot supports USD bookings.",
    "card_product_id": "Which credit card paid for the booking? Use the card catalog; never ask for a full card number.",
    "reason_category": "Why were you unable to take the trip?",
    "narrative": "Briefly describe what happened and when.",
    "other_insurance": "Do you have separate travel insurance for this trip: yes, no, or unsure?",
}
MEDICAL_QUESTIONS = {
    "medical.symptom_onset_date": "When did the symptoms or injury begin?",
    "medical.provider_seen": "Have you seen a healthcare provider about this illness or injury?",
    "medical.prior_condition": "Have you had this condition before?",
    "medical.stability_60day": "Were you treated for this condition in the 60 days before you booked the trip?",
}


def load_openapi() -> Optional[Dict[str, Any]]:
    """The schema this edition was built against, when the repo is checked out."""
    try:
        with open(OPENAPI_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        return None


def zoned(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(data: Any) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()


def new_key_token() -> str:
    return "tc_muse_" + base64.urlsafe_b64encode(secrets.token_bytes(36)).decode("ascii").rstrip("=")


def user_code() -> str:
    letters = "BCDFGHJKLMNPQRSTVWXZ"
    chars = [letters[b % len(letters)] for b in secrets.token_bytes(8)]
    return "".join(chars[:4]) + "-" + "".join(chars[4:])


@dataclass
class KeyRecord:
    token: str
    id: str
    channel: str  # "cli" for keys minted by tourclaim login; "key" for keys from /connect/muse or other apps
    traveler: str
    scopes: List[str]
    expires_at: datetime
    revoked: bool = False
    seq: int = 0  # creation order; at the key limit the oldest CLI key is retired


@dataclass
class DeviceRecord:
    device_code: str
    user_code: str
    client: str
    scopes: List[str]
    created_at: float
    expires_in: int
    interval: int
    status: str  # pending, approved, denied, consumed
    traveler: str
    description: Optional[str] = None
    last_poll: Optional[float] = None
    polls: int = 0


@dataclass
class IntakeRecord:
    id: str
    key_id: str
    traveler: str
    idempotency_key: str
    request_hash: str
    revision: int
    fields: Dict[str, Any]
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    approved_revision: Optional[int] = None
    claim_id: Optional[str] = None
    reads: int = 0
    approval_outdated: bool = False
    changed: int = 0


@dataclass
class ClaimRecord:
    id: str
    intake_id: str
    traveler: str
    booking_key: str
    status: str
    next_action: str
    updated_at: str
    seq: int


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    body: Any

    def q(self, name: str) -> Optional[str]:
        values = self.query.get(name)
        return values[-1] if values else None


class HttpError(Exception):
    def __init__(self, status: int, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(status)
        self.status = status
        self.body = body
        self.headers = headers or {}


_INVALID_JSON = object()


def fail(status: int, detail: Any, headers: Optional[Dict[str, str]] = None) -> HttpError:
    return HttpError(status, {"detail": detail}, headers)


def invalid(issues: List[Dict[str, Any]]) -> HttpError:
    return HttpError(422, {"detail": issues})


def issue(loc: List[Any], kind: str) -> Dict[str, Any]:
    return {"loc": loc, "type": kind, "msg": "Invalid field value"}


class MockServer:
    def __init__(
        self,
        *,
        enabled: bool = True,
        mode: str = "review",
        device_interval: int = 5,
        device_expires_in: int = 600,
        enforce_device_interval: bool = False,
        auto_approve_device_after_polls: Optional[int] = None,
        auto_sign_after_reads: Optional[int] = None,
        no_conflict_header: bool = False,
        relative_discovery: bool = False,
        legacy_schema: bool = False,
        send_complete_uri: bool = False,
        reject_bad_bearer: bool = False,
        traveler: str = "pat@example.com",
    ) -> None:
        self.enabled = enabled
        self.mode = mode
        self.device_interval = device_interval
        self.device_expires_in = device_expires_in
        self.enforce_device_interval = enforce_device_interval
        self.auto_approve_device_after_polls = auto_approve_device_after_polls
        self.auto_sign_after_reads = auto_sign_after_reads
        self.no_conflict_header = no_conflict_header
        self.relative_discovery = relative_discovery
        #: Behave like a server from before 1.57.1: no openapi-cli.json, and openapi.json lists every operation.
        self.legacy_schema = legacy_schema
        #: Behave like a server from before the security review: also send verification_uri_complete.
        self.send_complete_uri = send_complete_uri
        #: Answer 401 to a token poll whose bearer key is not a live key (the contract allows ignoring it instead).
        self.reject_bad_bearer = reject_bad_bearer
        self.traveler = traveler
        #: The traveler who approves new sign-ins (default: traveler).
        self.device_traveler: Optional[str] = None
        self.url = ""
        self.requests: List[RecordedRequest] = []
        self.keys: Dict[str, KeyRecord] = {}
        self.devices: Dict[str, DeviceRecord] = {}
        self.intakes: Dict[str, IntakeRecord] = {}
        self.claims: Dict[str, ClaimRecord] = {}
        self.cards = [
            {"id": 412, "name": "Sapphire Preferred", "issuer": "Chase"},
            {"id": 413, "name": "Sapphire Reserve", "issuer": "Chase"},
            {"id": 501, "name": "Venture X", "issuer": "Capital One"},
            {"id": 900, "name": "Synthetic Travel Card", "issuer": "Example Bank"},
        ]
        #: The next N connector requests answer 429 with this Retry-After.
        self.rate_limit: Dict[str, Any] = {"remaining": 0, "retry_after": "1"}
        #: Forced answers for upcoming token polls, such as ["pending", "slow_down", "approve"].
        self.device_script: List[str] = []
        #: Called for every request before it is handled.
        self.on_request: Optional[Callable[[RecordedRequest], None]] = None
        #: Responses returned, in order, to the next connector requests (to "path" only, when given) instead of handling them.
        self.inject: List[Dict[str, Any]] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._claim_seq = 0
        self._change_seq = 0
        self._key_seq = 0
        self.openapi = load_openapi() or {"openapi": "3.1.0", "info": {"title": "TourClaim by Copernican", "description": ""}, "paths": {}}

    # ---- lifecycle ----

    def start(self, port: int = 0) -> str:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet
                pass

            def _handle(self) -> None:
                mock._serve(self)

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def assistant_schema(self) -> Dict[str, Any]:
        """openapi.json: the full schema without the three operations only the CLI uses."""
        copy = json.loads(json.dumps(self.openapi))
        paths = copy.get("paths", {})
        paths.get("/api/connectors/v1/intakes", {}).pop("get", None)
        paths.pop("/api/connectors/v1/key", None)
        return copy

    # ---- test controls ----

    def issue_key(self, traveler: Optional[str] = None, scopes: Optional[List[str]] = None, expires_in: float = 30 * 86400, channel: str = "key") -> str:
        token = new_key_token()
        self._key_seq += 1
        self.keys[token] = KeyRecord(
            seq=self._key_seq,
            token=token,
            id=str(uuid.uuid4()),
            channel=channel,
            traveler=traveler or self.traveler,
            scopes=list(scopes or ALL_SCOPES),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )
        return token

    def key_record(self, token: str) -> Optional[KeyRecord]:
        return self.keys.get(token)

    def device(self, code: str) -> Optional[DeviceRecord]:
        return next((d for d in self.devices.values() if d.user_code == code), None)

    def sign(self, intake_id: str) -> None:
        """The traveler signs the draft's current revision."""
        rec = self.intakes[intake_id]
        if self.missing(rec.fields):
            raise AssertionError("cannot sign an incomplete draft")
        rec.approved_revision = rec.revision

    def requests_to(self, method: str, path: Any) -> List[RecordedRequest]:
        return [
            r for r in self.requests if r.method == method and (r.path == path if isinstance(path, str) else bool(path.search(r.path)))
        ]

    # ---- request handling ----

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        too_large = len(raw) > MAX_BODY
        body: Any = None
        if raw and not too_large:
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = _INVALID_JSON
        record = RecordedRequest(
            method=handler.command,
            path=parts.path,
            query=parse_qs(parts.query),
            headers={k.lower(): v for k, v in handler.headers.items()},
            body=body,
        )
        with self._lock:
            self.requests.append(record)
            if self.on_request:
                self.on_request(record)
            is_v1 = parts.path == "/api/connectors/v1" or parts.path.startswith("/api/connectors/v1/")
            headers = {"Cache-Control": "no-store"}
            if is_v1:
                headers["X-TourClaim-Mode"] = self.mode
            try:
                if too_large:
                    raise fail(413, "Request exceeds 8 MiB")
                if body is _INVALID_JSON:
                    raise invalid([issue(["body"], "json_invalid")])
                result = self._route(record)
                if result is None:
                    status, payload, content_type = 204, b"", None
                elif isinstance(result, str):
                    status, payload, content_type = 200, result.encode("utf-8"), "text/html; charset=utf-8"
                else:
                    status, payload, content_type = 200, json.dumps(result).encode("utf-8"), "application/json"
            except HttpError as error:
                headers.update(error.headers)
                status, payload, content_type = error.status, json.dumps(error.body).encode("utf-8"), "application/json"
            except Exception as error:  # noqa: BLE001 - report mock bugs as 500s
                status, payload, content_type = 500, json.dumps({"detail": f"mock error: {error!r}"}).encode("utf-8"), "application/json"
        handler.send_response(status)
        for name, value in headers.items():
            handler.send_header(name, value)
        if content_type:
            handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        if payload:
            handler.wfile.write(payload)

    def _conflict(self, code: str, detail: str) -> HttpError:
        return fail(409, detail, {} if self.no_conflict_header else {"X-TourClaim-Error": code})

    def _route(self, r: RecordedRequest) -> Any:
        method, path = r.method, r.path
        if path == "/api/connectors/v1" and method == "GET":
            base = "" if self.relative_discovery else self.url
            info = {
                "name": "TourClaim by Copernican",
                "version": "1.0.0",
                "enabled": self.enabled,
                "mode": self.mode,
                "openapi_url": f"{base}/api/connectors/v1/openapi.json",
                "connection_url": f"{base}/connect/muse",
                "documentation_url": f"{base}/muse/developers",
            }
            if not self.relative_discovery:
                info["cli_login_url"] = f"{base}/connect/cli"
                if not self.legacy_schema:
                    info["cli_openapi_url"] = f"{base}/api/connectors/v1/openapi-cli.json"
            return info
        if path == "/api/connectors/v1/openapi.json" and method == "GET":
            return self.openapi if self.legacy_schema else self.assistant_schema()
        if path == "/api/connectors/v1/openapi-cli.json" and method == "GET":
            if self.legacy_schema:
                raise fail(404, "Not Found")
            return self.openapi
        if path == "/connect/cli" and method == "GET":
            return self._browser_approve(r.q("code"))
        if path == "/connect/muse" and method == "GET":
            return self._browser_sign(r.q("intake"))

        if not path.startswith("/api/connectors/"):
            raise fail(404, "Not Found")
        if not self.enabled:
            raise fail(503, "Muse connector pilot is not enabled")
        if self.rate_limit["remaining"] > 0:
            self.rate_limit["remaining"] -= 1
            retry = self.rate_limit["retry_after"]
            raise HttpError(429, {"error": "Rate limit exceeded: 60 per 1 minute"}, {} if retry is None else {"Retry-After": retry})
        index = next((i for i, item in enumerate(self.inject) if not item.get("path") or item["path"] == path), None)
        if index is not None:
            injected = self.inject.pop(index)
            raise HttpError(injected["status"], injected["body"], injected.get("headers"))

        if path == "/api/connectors/device/code" and method == "POST":
            return self._device_code(r.body)
        if path == "/api/connectors/device/token" and method == "POST":
            return self._device_token(r.body, r.headers.get("authorization"))

        key = self._authenticate(r)
        v1 = path[len("/api/connectors/v1"):]
        if v1 == "/key" and method == "GET":
            # Only keys from tourclaim login say whose account they are; others get null, as from the server.
            return {
                "id": key.id,
                "expires_at": zoned(key.expires_at),
                "scopes": key.scopes,
                "account_email": key.traveler if key.channel == "cli" else None,
            }
        if v1 == "/key" and method == "DELETE":
            key.revoked = True
            return None
        if v1 == "/data-deletion" and method == "POST":
            # Asking deletes nothing: the server emails the traveler a link to confirm.
            self._scope(key, "intakes:write")
            if not isinstance(r.body, dict) or r.body.get("traveler_requested") is not True:
                raise invalid([issue(["body", "traveler_requested"], "literal_error")])
            return {
                "mode": "review", "id": "del-1", "status": "awaiting_confirmation", "email_sent": True,
                "expires_at": "2026-10-07T00:00:00Z", "next_action": "Tell the traveler to confirm from the email.",
                "deleted_on_confirmation": [], "handled_by_staff": [],
            }
        if v1 == "/cards" and method == "GET":
            self._scope(key, "intakes:write")
            q = r.q("q") or ""
            if len(q) > 100:
                raise invalid([issue(["query", "q"], "string_too_long")])
            needle = q.lower()
            matches = [c for c in self.cards if needle in f"{c['issuer']} {c['name']}".lower()]
            return [dict(c, coverage_status="requires_review") for c in matches[:30]]
        if v1 == "/intakes" and method == "GET":
            self._scope(key, "intakes:write")
            offset = self._offset(r)
            drafts = [i for i in self.intakes.values() if i.traveler == key.traveler and not i.claim_id and self._reaches(key, i)]
            drafts.sort(key=lambda i: -i.changed)
            return [self.intake_response(i) for i in drafts[offset : offset + 30]]
        if v1 == "/intakes" and method == "POST":
            self._scope(key, "intakes:write")
            return self._start_intake(key, r)
        m = re.fullmatch(r"/intakes/([^/]+)(/[a-z-]+)?", v1)
        if m:
            intake_id = unquote(m.group(1))
            action = m.group(2) or ""
            if action == "" and method == "GET":
                self._scope(key, "intakes:write")
                rec = self._owned(key, intake_id)
                self._maybe_auto_sign(rec)
                return self.intake_response(rec)
            if action == "" and method == "PATCH":
                self._scope(key, "intakes:write")
                return self._update(self._owned(key, intake_id), r.body)
            if action == "" and method == "DELETE":
                self._scope(key, "intakes:write")
                rec = self._owned(key, intake_id)
                if rec.claim_id:
                    raise self._conflict(
                        "intake_submitted",
                        "A submitted claim cannot be deleted here. See the data deletion instructions to request deletion.",
                    )
                del self.intakes[rec.id]
                return None
            if action == "/email-evidence" and method == "POST":
                self._scope(key, "evidence:write")
                return self._add_email(self._owned(key, intake_id), r.body)
            if action == "/attachments" and method == "POST":
                self._scope(key, "evidence:write")
                return self._add_attachment(self._owned(key, intake_id), r.body)
            if action == "/submit" and method == "POST":
                self._scope(key, "claims:submit")
                return self._submit(self._owned(key, intake_id), r.body)
        if v1 == "/claims" and method == "GET":
            self._scope(key, "claims:read")
            offset = self._offset(r)
            claims = sorted((c for c in self.claims.values() if c.traveler == key.traveler), key=lambda c: -c.seq)
            return [self._claim_response(c) for c in claims[offset : offset + 30]]
        m = re.fullmatch(r"/claims/([^/]+)", v1)
        if m and method == "GET":
            self._scope(key, "claims:read")
            claim = self.claims.get(unquote(m.group(1)))
            if not claim or claim.traveler != key.traveler:
                raise fail(404, "Claim not found")
            return self._claim_response(claim)
        raise fail(404, "Not Found")

    def _offset(self, r: RecordedRequest) -> int:
        text = r.q("offset") or "0"
        if not re.fullmatch(r"[0-9]+", text):
            raise invalid([issue(["query", "offset"], "int_parsing")])
        return int(text)

    # ---- device authorization ----

    def _device_code(self, body: Any) -> Any:
        b = body if isinstance(body, dict) else {}
        if not isinstance(b.get("client"), str) or not b["client"] or len(b["client"]) > 120:
            raise invalid([issue(["body", "client"], "missing")])
        scopes = ALL_SCOPES
        if "scopes" in b:
            if not isinstance(b["scopes"], list) or any(s not in ALL_SCOPES for s in b["scopes"]):
                raise invalid([issue(["body", "scopes"], "enum")])
            scopes = b["scopes"]
        record = DeviceRecord(
            device_code=secrets.token_urlsafe(32),
            user_code=user_code(),
            client=b["client"],
            scopes=list(scopes),
            created_at=time.time(),
            expires_in=self.device_expires_in,
            interval=self.device_interval,
            status="pending",
            traveler=self.device_traveler or self.traveler,
        )
        self.devices[record.device_code] = record
        page = f"{self.url}/connect/cli"
        response = {
            "device_code": record.device_code,
            "user_code": record.user_code,
            "verification_uri": page,
            "expires_in": record.expires_in,
            "interval": record.interval,
        }
        if self.send_complete_uri:
            response["verification_uri_complete"] = f"{page}?code={record.user_code}"
        return response

    def _device_token(self, body: Any, authorization: Optional[str] = None) -> Any:
        b = body if isinstance(body, dict) else {}
        if not isinstance(b.get("device_code"), str):
            raise HttpError(400, {"error": "invalid_request"})
        d = self.devices.get(b["device_code"])
        scripted = self.device_script.pop(0) if self.device_script else None
        if scripted == "pending":
            raise HttpError(400, {"error": "authorization_pending"})
        if scripted == "slow_down":
            raise HttpError(400, {"error": "slow_down"})
        if scripted == "denied":
            raise HttpError(400, {"error": "access_denied", "error_description": "The traveler declined this connection."})
        if scripted == "expired":
            raise HttpError(400, {"error": "expired_token"})
        if scripted == "approve" and d:
            d.status = "approved"
        # An approved code stays collectable for 60 seconds past its expiry.
        lifetime = (d.expires_in + (60 if d.status == "approved" else 0)) if d else 0
        if not d or d.status == "consumed" or time.time() > d.created_at + lifetime:
            raise HttpError(400, {"error": "expired_token"})
        now = time.time()
        if self.enforce_device_interval and d.last_poll is not None and now - d.last_poll < d.interval - 0.05:
            d.last_poll = now
            d.interval += 5
            raise HttpError(400, {"error": "slow_down"})
        d.last_poll = now
        d.polls += 1
        if d.status == "pending" and self.auto_approve_device_after_polls and d.polls >= self.auto_approve_device_after_polls:
            d.status = "approved"
        if d.status == "denied":
            raise HttpError(400, {"error": "access_denied", "error_description": d.description or "Declined."})
        if d.status == "pending":
            raise HttpError(400, {"error": "authorization_pending"})
        # A poll may carry the key this sign-in replaces. If it is a live CLI key of
        # the same traveler, it is retired as the new one is minted; otherwise it is ignored.
        now_dt = datetime.now(timezone.utc)
        m = re.fullmatch(r"Bearer (.+)", authorization or "")
        old = self.keys.get(m.group(1)) if m else None
        if m and self.reject_bad_bearer and (not old or old.revoked or old.expires_at <= now_dt):
            raise fail(401, "Muse connection expired or disconnected", {"WWW-Authenticate": "Bearer"})
        if old and old.channel == "cli" and old.traveler == d.traveler and not old.revoked and old.expires_at > now_dt:
            old.revoked = True
        # At the limit of five connections, retire the traveler's oldest CLI key;
        # keys made for other apps are never touched.
        active = [k for k in self.keys.values() if k.traveler == d.traveler and not k.revoked and k.expires_at > now_dt]
        if len(active) >= 5:
            cli_keys = sorted((k for k in active if k.channel == "cli"), key=lambda k: k.seq)
            if not cli_keys:
                d.status = "denied"
                raise HttpError(
                    400,
                    {
                        "error": "access_denied",
                        "error_description": "This account already has five connections. Disconnect one at /connect/muse, then run `tourclaim login` again.",
                    },
                )
            cli_keys[0].revoked = True
        d.status = "consumed"
        token = self.issue_key(traveler=d.traveler, scopes=d.scopes, channel="cli")
        key = self.keys[token]
        return {"api_key": token, "expires_at": zoned(key.expires_at), "scopes": key.scopes, "grant_id": key.id}

    def _browser_approve(self, code: Optional[str]) -> str:
        if code is None:
            return (
                '<!doctype html><title>TourClaim mock</title><form><label>Enter the code shown in your terminal '
                '<input name="code" autocomplete="off"></label> <button>Continue</button></form>'
            )
        d = self.device(code.strip().upper())
        if not d or d.status != "pending":
            return "<!doctype html><title>TourClaim mock</title><p>No pending sign-in with that code.</p>"
        d.status = "approved"
        return f"<!doctype html><title>TourClaim mock</title><p>Mock: approved sign-in {d.user_code} for {d.traveler}. Return to the terminal.</p>"

    def _browser_sign(self, intake_id: Optional[str]) -> str:
        rec = self.intakes.get(intake_id or "")
        if not rec or rec.claim_id or self.missing(rec.fields):
            return "<!doctype html><title>TourClaim mock</title><p>Nothing to sign for that draft.</p>"
        rec.approved_revision = rec.revision
        return f"<!doctype html><title>TourClaim mock</title><p>Mock: the traveler signed revision {rec.revision} of draft {rec.id}.</p>"

    # ---- auth ----

    def _authenticate(self, r: RecordedRequest) -> KeyRecord:
        header = r.headers.get("authorization", "")
        m = re.fullmatch(r"Bearer (.+)", header)

        def unauthorized(detail: str) -> HttpError:
            return fail(401, detail, {"WWW-Authenticate": "Bearer"})

        if not m or not m.group(1).startswith("tc_muse_"):
            raise unauthorized("Muse connection required")
        key = self.keys.get(m.group(1))
        if not key or key.revoked or key.expires_at < datetime.now(timezone.utc):
            raise unauthorized("Muse connection expired or disconnected")
        return key

    def _scope(self, key: KeyRecord, name: str) -> None:
        if name not in key.scopes:
            raise fail(403, "Connection does not permit this action")

    def _reaches(self, key: KeyRecord, rec: IntakeRecord) -> bool:
        """A key reaches the drafts it started. A CLI key also reaches drafts any
        of the same traveler's CLI keys started, even revoked or expired ones."""
        if rec.key_id == key.id:
            return True
        starter = next((k for k in self.keys.values() if k.id == rec.key_id), None)
        return key.channel == "cli" and starter is not None and starter.channel == "cli" and starter.traveler == key.traveler

    def _owned(self, key: KeyRecord, intake_id: str) -> IntakeRecord:
        rec = self.intakes.get(intake_id)
        if not rec or rec.traveler != key.traveler or not self._reaches(key, rec):
            raise fail(404, "Intake not found for this connection")
        return rec

    # ---- intakes ----

    def _validate_fields(self, fields: Any, loc: List[Any]) -> List[Dict[str, Any]]:
        if fields is None:
            return []
        if not isinstance(fields, dict):
            return [issue(loc, "model_type")]
        issues: List[Dict[str, Any]] = []

        def text(k: str, v: Any, low: int, high: int) -> None:
            if not isinstance(v, str):
                issues.append(issue(loc + [k], "string_type"))
            elif len(v) < low:
                issues.append(issue(loc + [k], "string_too_short"))
            elif len(v) > high:
                issues.append(issue(loc + [k], "string_too_long"))

        for k, v in fields.items():
            if v is None:
                continue
            if k == "merchant_name":
                text(k, v, 1, 255)
            elif k == "booking_ref":
                text(k, v, 1, 100)
            elif k == "trip_date":
                if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    issues.append(issue(loc + [k], "date_from_datetime_parsing"))
            elif k in ("booking_amount", "refunded_amount"):
                ok = (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0) or (isinstance(v, str) and DECIMAL.fullmatch(v))
                if not ok:
                    issues.append(issue(loc + [k], "decimal_parsing"))
            elif k == "currency":
                if v != "USD":
                    issues.append(issue(loc + [k], "literal_error"))
            elif k == "card_product_id":
                if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                    issues.append(issue(loc + [k], "int_type"))
            elif k == "reason_category":
                if v not in REASONS:
                    issues.append(issue(loc + [k], "enum"))
            elif k == "narrative":
                text(k, v, 10, 10000)
            elif k == "cancellation_policy":
                text(k, v, 0, 10000)
            elif k == "other_insurance":
                if v not in ("YES", "NO", "UNSURE"):
                    issues.append(issue(loc + [k], "enum"))
            elif k == "other_insurance_details":
                text(k, v, 0, 500)
            elif k == "medical":
                if not isinstance(v, dict):
                    issues.append(issue(loc + [k], "model_type"))
                    continue
                for mk, mv in v.items():
                    if mv is None:
                        continue
                    if mk == "symptom_onset_date":
                        if not isinstance(mv, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", mv):
                            issues.append(issue(loc + [k, mk], "date_from_datetime_parsing"))
                    elif mk in ("provider_seen", "prior_condition", "stability_60day"):
                        if not isinstance(mv, bool):
                            issues.append(issue(loc + [k, mk], "bool_type"))
                    elif mk in ("provider_details", "prior_condition_details", "stability_60day_details"):
                        if not isinstance(mv, str) or len(mv) > 2000:
                            issues.append(issue(loc + [k, mk], "string_type"))
                    else:
                        issues.append(issue(loc + [k, mk], "extra_forbidden"))
            else:
                issues.append(issue(loc + [k], "extra_forbidden"))
        return issues

    @staticmethod
    def _normalize(fields: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(fields)
        for k in ("booking_amount", "refunded_amount"):
            if isinstance(out.get(k), (int, float)) and not isinstance(out.get(k), bool):
                out[k] = f"{out[k]:.2f}"
        return out

    @staticmethod
    def _refund_too_high(fields: Dict[str, Any]) -> bool:
        if fields.get("booking_amount") is None or fields.get("refunded_amount") is None:
            return False
        return float(fields["refunded_amount"]) > float(fields["booking_amount"])

    def _check_card(self, fields: Dict[str, Any]) -> None:
        card = fields.get("card_product_id")
        if card is not None and not any(c["id"] == card for c in self.cards):
            raise fail(422, "Unknown card_product_id; search the card catalog")

    def missing(self, fields: Dict[str, Any]) -> List[str]:
        missing = [k for k in REQUIRED_FIELDS if fields.get(k) is None]
        if fields.get("other_insurance") == "YES" and not fields.get("other_insurance_details"):
            missing.append("other_insurance_details")
        if fields.get("reason_category") in ("ILLNESS", "INJURY"):
            medical = fields.get("medical") or {}
            for name in MEDICAL_QUESTIONS:
                if medical.get(name.split(".")[1]) is None:
                    missing.append(name)
        return missing

    def intake_response(self, rec: IntakeRecord) -> Dict[str, Any]:
        missing = self.missing(rec.fields)
        if rec.claim_id:
            state = "submitted"
        elif missing:
            state = "collecting"
        elif rec.approved_revision == rec.revision and not rec.approval_outdated:
            state = "ready_to_submit"
        else:
            state = "needs_approval"
        counts: Dict[str, int] = {}
        evidence = []
        for e in rec.evidence:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            label = f"{'Email' if e['kind'] == 'email' else 'File'} evidence {counts[e['kind']]}"
            evidence.append({"id": e["id"], "kind": e["kind"], "label": label})
        medical = rec.fields.get("reason_category") in ("ILLNESS", "INJURY")
        link = f"{self.url}/connect/muse?intake={rec.id}"
        fields = {k: rec.fields.get(k) for k in list(REQUIRED_FIELDS) + ["cancellation_policy", "other_insurance_details", "medical"]}
        questions = dict(REQUIRED_FIELDS, **MEDICAL_QUESTIONS)
        return {
            "mode": self.mode,
            "id": rec.id,
            "revision": rec.revision,
            "fields": fields,
            "missing_fields": missing,
            "next_questions": [questions.get(n, "Which insurer and what does the policy cover?") for n in missing][:3],
            "evidence": evidence,
            "state": state,
            "review_url": link if state == "needs_approval" else None,
            "evidence_upload_url": None if state == "submitted" else link,
            "claim_id": rec.claim_id,
            "coverage_status": "not_determined",
            "medical_note_pathway": (
                "Copernican can arrange medical documentation review; a provider determines whether a note can be issued."
                if medical
                else "Medical documentation requirements will be reviewed by Copernican."
            ),
        }

    def _start_intake(self, key: KeyRecord, r: RecordedRequest) -> Any:
        idem = r.headers.get("idempotency-key")
        issues: List[Dict[str, Any]] = []
        if not isinstance(idem, str):
            issues.append(issue(["header", "idempotency-key"], "missing"))
        elif not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", idem):
            issues.append(issue(["header", "idempotency-key"], "string_pattern_mismatch"))
        body = r.body if r.body is not None else {}
        if not isinstance(body, dict):
            raise invalid([issue(["body"], "model_type")])
        for k in body:
            if k != "fields":
                issues.append(issue(["body", k], "extra_forbidden"))
        issues += self._validate_fields(body.get("fields"), ["body", "fields"])
        if issues:
            raise invalid(issues)
        fields = self._normalize(body.get("fields") or {})
        if self._refund_too_high(fields):
            raise invalid([issue(["body", "fields"], "value_error")])
        request_hash = sha256(json.dumps(fields, sort_keys=True))
        existing = next((i for i in self.intakes.values() if i.traveler == key.traveler and i.idempotency_key == idem), None)
        if existing:
            if existing.request_hash != request_hash:
                raise self._conflict("idempotency_key_reused", "Idempotency key was already used with different fields")
            if not self._reaches(key, existing):
                raise self._conflict(
                    "idempotency_key_other_connection", "This intake belongs to an earlier connection; use a new idempotency key"
                )
            return self.intake_response(existing)
        self._check_card(fields)
        self._change_seq += 1
        rec = IntakeRecord(
            id=str(uuid.uuid4()),
            key_id=key.id,
            traveler=key.traveler,
            idempotency_key=idem or "",
            request_hash=request_hash,
            revision=1,
            fields=fields,
            changed=self._change_seq,
        )
        self.intakes[rec.id] = rec
        return self.intake_response(rec)

    @staticmethod
    def _check_revision_body(body: Any, allowed: List[str]) -> Dict[str, Any]:
        b = body if isinstance(body, dict) else {}
        issues = []
        rev = b.get("expected_revision")
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
            issues.append(issue(["body", "expected_revision"], "int_type"))
        for k in b:
            if k not in allowed and k != "expected_revision":
                issues.append(issue(["body", k], "extra_forbidden"))
        if issues:
            raise invalid(issues)
        return b

    def _change_revision(self, rec: IntakeRecord, expected: int) -> None:
        if rec.claim_id:
            raise self._conflict("intake_submitted", "Submitted intake is read-only")
        if rec.revision != expected:
            raise self._conflict("stale_revision", "Intake changed; retrieve the current revision and try again")
        rec.revision += 1
        rec.approved_revision = None
        self._change_seq += 1
        rec.changed = self._change_seq

    def _update(self, rec: IntakeRecord, body: Any) -> Any:
        b = self._check_revision_body(body, ["fields"])
        if "fields" not in b:
            raise invalid([issue(["body", "fields"], "missing")])
        issues = self._validate_fields(b["fields"], ["body", "fields"])
        if issues:
            raise invalid(issues)
        patch = self._normalize(b["fields"])
        if isinstance(patch.get("medical"), dict):
            patch["medical"] = dict(rec.fields.get("medical") or {}, **patch["medical"])
        merged = dict(rec.fields, **patch)
        if self._refund_too_high(merged):
            raise fail(422, "Updated fields are inconsistent; refund cannot exceed amount paid")
        self._check_card(merged)
        self._change_revision(rec, b["expected_revision"])
        rec.fields = merged
        return self.intake_response(rec)

    def _add_evidence(self, rec: IntakeRecord, revision: int, kind: str, source_hash: str, data: str) -> Any:
        previous = next((e for e in rec.evidence if e["source_hash"] == source_hash), None)
        if previous:
            if previous["data"] != data:
                raise self._conflict("evidence_conflict", "Evidence source was already imported with different contents")
            return self.intake_response(rec)
        if len(rec.evidence) >= 30:
            raise fail(422, "An intake supports at most 30 evidence items")
        self._change_revision(rec, revision)
        rec.evidence.append({"id": str(uuid.uuid4()), "kind": kind, "source_hash": source_hash, "data": data})
        return self.intake_response(rec)

    def _add_email(self, rec: IntakeRecord, body: Any) -> Any:
        allowed = ["provider", "message_id", "subject", "sender", "sent_at", "text", "user_authorized_sharing"]
        b = self._check_revision_body(body, allowed)
        issues = []
        if b.get("provider") not in ("gmail", "outlook", "user"):
            issues.append(issue(["body", "provider"], "enum"))
        if not isinstance(b.get("message_id"), str) or not b["message_id"] or len(b["message_id"]) > 255:
            issues.append(issue(["body", "message_id"], "string_type"))
        if not isinstance(b.get("subject"), str) or len(b["subject"]) > 500:
            issues.append(issue(["body", "subject"], "string_type"))
        if not isinstance(b.get("sender"), str) or len(b["sender"]) > 320:
            issues.append(issue(["body", "sender"], "string_type"))
        sent_at = b.get("sent_at")
        if sent_at is not None:
            try:
                datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            except ValueError:
                issues.append(issue(["body", "sent_at"], "datetime_parsing"))
        if not isinstance(b.get("text"), str) or not b["text"] or len(b["text"]) > 30000:
            issues.append(issue(["body", "text"], "string_type"))
        if b.get("user_authorized_sharing") is not True:
            issues.append(issue(["body", "user_authorized_sharing"], "literal_error"))
        if issues:
            raise invalid(issues)
        data = json.dumps([b["provider"], b["message_id"], b["subject"], b["sender"], sent_at, b["text"]])
        return self._add_evidence(rec, b["expected_revision"], "email", sha256(f"{b['provider']}:{b['message_id']}"), data)

    def _add_attachment(self, rec: IntakeRecord, body: Any) -> Any:
        allowed = ["filename", "content_type", "doc_type", "content_base64", "user_authorized_sharing"]
        b = self._check_revision_body(body, allowed)
        issues = []
        filename = b.get("filename")
        if not isinstance(filename, str) or not re.fullmatch(r"[^/\\\x00-\x1f]+", filename) or len(filename) > 200:
            issues.append(issue(["body", "filename"], "string_pattern_mismatch"))
        if b.get("content_type") not in MAGIC:
            issues.append(issue(["body", "content_type"], "enum"))
        if b.get("doc_type") not in ("receipt", "itinerary", "medical_note", "other"):
            issues.append(issue(["body", "doc_type"], "enum"))
        content_b64 = b.get("content_base64")
        if not isinstance(content_b64, str) or len(content_b64) < 4 or len(content_b64) > 6990508:
            issues.append(issue(["body", "content_base64"], "string_type"))
        if not isinstance(b.get("user_authorized_sharing"), bool):
            issues.append(issue(["body", "user_authorized_sharing"], "bool_type"))
        if issues:
            raise invalid(issues)
        if b["user_authorized_sharing"] is not True:
            raise fail(422, "Ask the traveler before sharing evidence")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except ValueError:
            raise fail(422, "Attachment must be valid base64") from None
        if len(content) > 5 * 1024 * 1024 or not content.startswith(MAGIC[b["content_type"]]):
            raise fail(422, "Attachment size or file type is invalid")
        data = json.dumps([filename, b["content_type"], b["doc_type"]])
        return self._add_evidence(rec, b["expected_revision"], "attachment", sha256(content), data)

    def _maybe_auto_sign(self, rec: IntakeRecord) -> None:
        after = self.auto_sign_after_reads
        if not after or rec.claim_id or self.missing(rec.fields) or rec.approved_revision == rec.revision:
            return
        rec.reads += 1
        if rec.reads >= after:
            rec.approved_revision = rec.revision
            rec.reads = 0

    def _submit(self, rec: IntakeRecord, body: Any) -> Any:
        b = self._check_revision_body(body, [])
        if rec.claim_id:
            return self._claim_response(self.claims[rec.claim_id])
        # The server's order: each cause has its own code, in the order a client should fix them.
        if rec.revision != b["expected_revision"]:
            raise self._conflict("stale_revision", "Intake changed; retrieve the current revision and try again")
        if rec.approved_revision != rec.revision:
            raise self._conflict("approval_required", "The traveler must approve this exact intake revision")
        if self.missing(rec.fields):
            raise self._conflict("intake_incomplete", "Intake is incomplete")
        if rec.approval_outdated:  # set by tests: signature over 7 days old, or the authorization changed
            raise self._conflict("approval_outdated", "Approval expired; ask the traveler to review again")
        if float(rec.fields.get("booking_amount") or 0) - float(rec.fields.get("refunded_amount") or 0) <= 0:
            raise fail(422, "There is no unreimbursed booking cost to claim")
        booking_key = "|".join(
            [rec.traveler, str(rec.fields.get("merchant_name")).lower(), str(rec.fields.get("booking_ref")).lower(), str(rec.fields.get("trip_date"))]
        )
        if any(c.booking_key == booking_key for c in self.claims.values()):
            raise self._conflict("duplicate_booking", "A claim for this booking already exists")
        self._claim_seq += 1
        claim = ClaimRecord(
            id=str(uuid.uuid4()),
            intake_id=rec.id,
            traveler=rec.traveler,
            booking_key=booking_key,
            status="INTAKE_RECEIVED",
            next_action=(
                "Synthetic review claim received. Nothing will be filed."
                if self.mode == "review"
                else "Copernican is reviewing your claim and evidence."
            ),
            updated_at=zoned(datetime.now(timezone.utc)),
            seq=self._claim_seq,
        )
        self.claims[claim.id] = claim
        rec.claim_id = claim.id
        return self._claim_response(claim)

    def _claim_response(self, c: ClaimRecord) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "id": c.id,
            "intake_id": c.intake_id,
            "status": c.status,
            "next_action": c.next_action,
            "updated_at": c.updated_at,
        }


def main() -> None:
    """Runs the mock on http://127.0.0.1:4010 (or MOCK_PORT) for trying the CLI by hand.

    Typing the code into /connect/cli approves a sign-in (it is also approved on
    its own after two polls), and opening a draft's review link signs it.
    """
    import sys

    mock = MockServer(device_interval=2, enforce_device_interval=True, auto_approve_device_after_polls=2)
    url = mock.start(int(os.environ.get("MOCK_PORT", "4010")))
    sys.stderr.write(
        "\n".join(
            [
                f"Mock TourClaim API on {url} (review mode, synthetic data).",
                "Try:",
                f"  export TOURCLAIM_API_URL={url}",
                "  tourclaim login --no-browser",
                "Open (or curl) a draft's review link to sign it as the traveler.",
                "Press Ctrl-C to stop.",
                "",
            ]
        )
    )
    sys.stderr.flush()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        mock.stop()


if __name__ == "__main__":
    main()
