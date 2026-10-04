"""The library: one method per operation, typed exceptions, device login."""

import itertools
import re
import socket
import threading

import pytest
from conftest import PDF, PNG

import tourclaim
from tourclaim import (
    AccessDeniedError,
    APIError,
    AuthenticationError,
    AuthorizationPendingError,
    Client,
    ConflictError,
    ConsentRequiredError,
    ExpiredTokenError,
    FileTooLargeError,
    NetworkError,
    NotFoundError,
    NotSignedInError,
    PermissionDeniedError,
    RateLimitError,
    RequestTimeoutError,
    SignatureTimeoutError,
    TourClaimError,
    UnavailableError,
    UnsupportedFileError,
    UsageError,
    ValidationError,
)

COMPLETE = {
    "merchant_name": "Example Air",
    "booking_ref": "EXA-1",
    "trip_date": "2026-03-04",
    "booking_amount": "250.00",
    "refunded_amount": "0.00",
    "currency": "USD",
    "card_product_id": 412,
    "reason_category": "WEATHER",
    "narrative": "The harbor closed for a storm warning.",
    "other_insurance": "NO",
}
_n = itertools.count(1)


@pytest.fixture(autouse=True)
def reset(mock):
    mock.device_script = []
    mock.rate_limit = {"remaining": 0, "retry_after": "1"}
    mock.inject = []
    mock.on_request = None
    mock.enabled = True


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Point the standard credentials location at a temporary directory."""
    for name in ("TOURCLAIM_API_KEY", "TOURCLAIM_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(mock):
    traveler = f"lib{next(_n)}@example.com"
    c = Client(mock.issue_key(traveler=traveler, channel="cli"), mock.url, sleep=lambda s: None)
    c.traveler = traveler
    return c


def test_exports_version_and_errors():
    assert tourclaim.__version__ == "0.1.0"
    assert issubclass(ConflictError, APIError) and issubclass(APIError, TourClaimError)
    assert issubclass(UsageError, ValueError) and issubclass(ConsentRequiredError, ValueError)
    for name in tourclaim.__all__:
        assert hasattr(tourclaim, name), name


def test_full_claim_through_the_library(client, mock):
    draft = client.start_intake({"merchant_name": "Example Air"})
    assert draft["state"] == "collecting" and client.mode == "review"
    assert [d["id"] for d in client.list_drafts()] == [draft["id"]]
    draft = client.update_intake(draft["id"], {k: v for k, v in COMPLETE.items() if k != "merchant_name"}, expected_revision=1)
    assert draft["state"] == "needs_approval"
    draft = client.add_attachment(draft["id"], expected_revision=draft["revision"], filename="r.pdf", content=PDF, doc_type="receipt", user_authorized_sharing=True)
    draft = client.add_email(
        draft["id"],
        expected_revision=draft["revision"],
        subject="Cancelled",
        sender="ops@example-air.test",
        text="Your flight was cancelled.",
        sent_at="2026-03-03T09:05:00-05:00",
        user_authorized_sharing=True,
    )
    body = mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft['id']}/email-evidence")[-1].body
    assert body["sent_at"] == "2026-03-03T14:05:00.000Z" and body["message_id"].startswith("sha256-")
    assert len(draft["evidence"]) == 2

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            mock.sign(draft["id"])

    signed = client.wait_for_signature(draft["id"], sleep=sleep)
    assert signed["state"] == "ready_to_submit" and sleeps == [5, 5]
    claim = client.submit(draft["id"], expected_revision=signed["revision"])
    assert claim["status"] == "INTAKE_RECEIVED"
    assert client.submit(draft["id"], expected_revision=signed["revision"])["id"] == claim["id"], "submit is safe to retry"
    assert [c["id"] for c in client.list_claims()] == [claim["id"]]
    assert client.get_claim(claim["id"])["intake_id"] == draft["id"]
    assert client.list_drafts() == []
    assert client.search_cards("venture")[0]["id"] == 501
    info = client.get_connection()
    assert info["account_email"] == client.traveler
    client.disconnect()
    with pytest.raises(AuthenticationError) as caught:
        client.get_connection()
    assert caught.value.status == 401 and caught.value.code == "unauthorized" and caught.value.exit_code == 3


def test_discovery_and_schema_need_no_key(mock):
    c = Client(api_url=mock.url, load_credentials=False)
    assert c.connector_info()["cli_login_url"] == f"{mock.url}/connect/cli"
    full = c.schema()
    assert full["openapi"] == "3.1.0" and "/api/connectors/v1/key" in full["paths"]
    assert mock.requests[-1].path == "/api/connectors/v1/openapi-cli.json"
    assert c.connector_info()["cli_openapi_url"] == f"{mock.url}/api/connectors/v1/openapi-cli.json"
    assert c.mode == "review"
    assert not c.has_api_key
    with pytest.raises(NotSignedInError) as caught:
        c.list_claims()
    assert caught.value.code == "not_signed_in" and caught.value.exit_code == 3


def test_key_precedence_argument_then_env_then_stored(mock, isolated_env, monkeypatch):
    stored = mock.issue_key(traveler="stored@example.com", channel="cli")
    from_env = mock.issue_key(traveler="env@example.com", channel="cli")
    explicit = mock.issue_key(traveler="arg@example.com", channel="cli")
    tourclaim.CredentialStore.default().set(mock.url, tourclaim.StoredCredential(stored))
    assert Client(api_url=mock.url).get_connection()["account_email"] == "stored@example.com"
    monkeypatch.setenv("TOURCLAIM_API_KEY", from_env)
    c = Client(api_url=mock.url)
    assert c.get_connection()["account_email"] == "env@example.com" and c.key_source == "env"
    assert Client(explicit, mock.url).get_connection()["account_email"] == "arg@example.com"
    monkeypatch.setenv("TOURCLAIM_API_URL", mock.url)
    assert Client().api_url == mock.url


def test_repr_never_shows_the_key(mock):
    key = mock.issue_key()
    c = Client(key, mock.url)
    assert key not in repr(c) and "set" in repr(c)
    assert key not in repr(tourclaim.StoredCredential(key, None, None, []))


def test_typed_exceptions_carry_status_code_and_message(client, mock):
    with pytest.raises(NotFoundError) as nf:
        client.get_intake("nope")
    assert (nf.value.status, nf.value.code, nf.value.exit_code) == (404, "not_found", 6)
    assert nf.value.detail == "Intake not found for this connection"

    draft = client.start_intake()
    with pytest.raises(ConflictError) as conflict:
        client.update_intake(draft["id"], {"booking_ref": "X"}, expected_revision=7)
    assert (conflict.value.status, conflict.value.code, conflict.value.exit_code) == (409, "stale_revision", 4)
    assert conflict.value.headers["x-tourclaim-error"] == "stale_revision"

    with pytest.raises(ValidationError) as invalid:
        client.update_intake(draft["id"], {"narrative": "short"}, expected_revision=1)
    assert invalid.value.status == 422 and invalid.value.detail[0]["type"] == "string_too_short"

    mock.rate_limit = {"remaining": 2, "retry_after": "3"}
    with pytest.raises(RateLimitError) as limited:
        client.list_claims()
    assert limited.value.retry_after == 3 and limited.value.exit_code == 5

    read_only = Client(mock.issue_key(scopes=["claims:read"]), mock.url)
    with pytest.raises(PermissionDeniedError) as forbidden:
        read_only.search_cards("x")
    assert forbidden.value.code == "forbidden"

    mock.enabled = False
    with pytest.raises(UnavailableError) as unavailable:
        client.list_claims()
    assert unavailable.value.exit_code == 7


def test_start_intake_errors_carry_the_idempotency_key(client, mock):
    client.start_intake({"booking_ref": "A"}, idempotency_key="lib-retry-0001")
    with pytest.raises(ConflictError) as caught:
        client.start_intake({"booking_ref": "B"}, idempotency_key="lib-retry-0001")
    assert caught.value.code == "idempotency_key_reused"
    assert caught.value.extra["idempotency_key"] == "lib-retry-0001"
    with pytest.raises(UsageError):
        client.start_intake(idempotency_key="short")


def test_evidence_needs_explicit_consent_and_valid_files(client, mock):
    draft = client.start_intake()
    before = len(mock.requests)
    with pytest.raises(ConsentRequiredError):
        client.add_attachment(draft["id"], expected_revision=1, filename="r.png", content=PNG, doc_type="receipt", user_authorized_sharing=False)
    with pytest.raises(ConsentRequiredError):
        client.add_email(draft["id"], expected_revision=1, subject="s", sender="a@b", text="hello", user_authorized_sharing="yes")  # type: ignore[arg-type]
    with pytest.raises(UnsupportedFileError):
        client.add_attachment(draft["id"], expected_revision=1, filename="r.png", content=b"GIF89a", doc_type="receipt", user_authorized_sharing=True)
    with pytest.raises(UnsupportedFileError):
        client.add_attachment(draft["id"], expected_revision=1, filename="r.png", content=PNG, doc_type="receipt", content_type="application/pdf", user_authorized_sharing=True)
    with pytest.raises(FileTooLargeError):
        client.add_attachment(draft["id"], expected_revision=1, filename="r.png", content=PNG + bytes(5 * 1024 * 1024), doc_type="receipt", user_authorized_sharing=True)
    with pytest.raises(UsageError):
        client.add_attachment(draft["id"], expected_revision=1, filename="a/b.png", content=PNG, doc_type="receipt", user_authorized_sharing=True)
    with pytest.raises(UsageError):
        client.add_email(draft["id"], expected_revision=1, subject="s", sender="a@b", text="hi", sent_at="2026-03-03 14:05", user_authorized_sharing=True)
    assert len(mock.requests) == before, "nothing is sent when a check fails"


def test_wait_for_signature_times_out_or_reports_missing_answers(client, mock):
    draft = client.start_intake(COMPLETE)
    clock = [1000.0]

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(SignatureTimeoutError) as caught:
        client.wait_for_signature(draft["id"], timeout=12, sleep=sleep, now=lambda: clock[0])
    assert caught.value.code == "timeout" and caught.value.extra["review_url"].endswith(draft["id"])
    incomplete = client.start_intake({"merchant_name": "Half"})
    with pytest.raises(ConflictError) as conflict:
        client.wait_for_signature(incomplete["id"], sleep=sleep, now=lambda: clock[0])
    assert conflict.value.code == "intake_incomplete"


def test_device_login_polls_saves_and_uses_the_new_key(mock, isolated_env):
    mock.device_script = ["pending", "slow_down", "approve"]
    shown = []
    sleeps = []
    c = Client(api_url=mock.url)
    token = c.device_login(["claims:read"], on_code=shown.append, save=True, sleep=sleeps.append)
    assert shown[0]["user_code"] and "device_code" in shown[0]
    assert sleeps == [5, 5, 10]
    assert token["api_key"].startswith("tc_muse_") and token["scopes"] == ["claims:read"]
    assert c.key_source == "device_login" and c.get_connection()["scopes"] == ["claims:read"]
    stored = tourclaim.CredentialStore.default().get(mock.url)
    assert stored.api_key == token["api_key"] and stored.grant_id == token["grant_id"]
    assert Client(api_url=mock.url).get_connection()["id"] == token["grant_id"]


def test_device_login_prints_the_code_to_stderr_by_default(mock, capsys):
    mock.device_script = ["approve"]
    Client(api_url=mock.url, load_credentials=False).device_login(sleep=lambda s: None)
    err = capsys.readouterr().err
    assert f"open {mock.url}/connect/cli\n" in err
    assert re.search(r"^Enter this code on that page: [A-Z]{4}-[A-Z]{4}$", err, re.M)
    assert "?code=" not in err and "tc_muse_" not in err


def test_account_email_only_for_login_keys(mock):
    assert Client(mock.issue_key(channel="key"), mock.url).get_connection().get("account_email") is None
    assert Client(mock.issue_key(channel="cli"), mock.url).get_connection()["account_email"] == mock.traveler


def test_device_code_has_no_complete_uri_even_from_older_servers():
    from mock_server import MockServer

    old = MockServer(send_complete_uri=True)
    old.start()
    try:
        code = Client(api_url=old.url, load_credentials=False).request_device_code()
        assert "verification_uri_complete" not in code
        assert code["verification_uri"] == f"{old.url}/connect/cli"
    finally:
        old.stop()


def test_device_flow_errors(mock):
    c = Client(api_url=mock.url, load_credentials=False)
    code = c.request_device_code()
    mock.device_script = ["pending"]
    with pytest.raises(AuthorizationPendingError):
        c.poll_device_token(code["device_code"])
    mock.device_script = ["denied"]
    with pytest.raises(AccessDeniedError) as denied:
        c.wait_for_device_token(code, sleep=lambda s: None)
    assert denied.value.exit_code == 3 and "declined" in denied.value.message
    mock.device_script = ["expired"]
    with pytest.raises(ExpiredTokenError):
        c.wait_for_device_token(code, sleep=lambda s: None)
    with pytest.raises(UsageError):
        c.request_device_code(["admin"])


def test_redirects_are_not_followed(client, mock):
    mock.inject.append({"status": 302, "body": {}, "headers": {"Location": "https://elsewhere.example/steal"}})
    with pytest.raises(APIError) as caught:
        client.list_claims()
    assert caught.value.code == "redirect" and "elsewhere.example" in caught.value.message
    assert len([r for r in mock.requests if r.path == "/steal"]) == 0


def test_network_errors_and_timeouts():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(NetworkError) as caught:
        Client("tc_muse_x", f"http://127.0.0.1:{port}").list_claims()
    assert caught.value.code == "network_error" and "Could not reach" in caught.value.message

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    accepted = []
    threading.Thread(target=lambda: accepted.append(silent.accept()), daemon=True).start()
    try:
        with pytest.raises(RequestTimeoutError) as timed_out:
            Client("tc_muse_x", f"http://127.0.0.1:{silent.getsockname()[1]}", timeout=0.5).list_claims()
        assert timed_out.value.code == "timeout"
    finally:
        for conn, _ in accepted:
            conn.close()
        silent.close()


def test_rejects_unsafe_api_urls():
    with pytest.raises(UsageError):
        Client("tc_muse_x", "http://api.example.com")


def test_device_login_save_sends_the_stored_key_so_the_server_retires_it(mock, isolated_env):
    old = mock.issue_key(channel="cli")
    tourclaim.CredentialStore.default().set(mock.url, tourclaim.StoredCredential(old))
    mock.device_script = ["approve"]
    Client(api_url=mock.url, load_credentials=False).device_login(save=True, sleep=lambda s: None, on_code=lambda code: None)
    assert mock.requests_to("POST", "/api/connectors/device/token")[-1].headers.get("authorization") == f"Bearer {old}"
    assert mock.key_record(old).revoked is True
    assert tourclaim.CredentialStore.default().get(mock.url).api_key != old


def test_wait_for_signature_rides_out_temporary_server_errors(client, mock):
    draft = client.start_intake(COMPLETE)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            mock.inject.append({"status": 503, "body": {"detail": "Service busy"}, "headers": {"Retry-After": "1"}})
        if len(sleeps) == 2:
            mock.inject.append({"status": 502, "body": ""})
        if len(sleeps) == 3:
            mock.sign(draft["id"])

    assert client.wait_for_signature(draft["id"], sleep=sleep)["state"] == "ready_to_submit"
    assert sleeps == [5, 1, 5]


def test_wait_for_signature_ends_after_repeated_temporary_errors(client, mock):
    draft = client.start_intake(COMPLETE)
    with pytest.raises(UnavailableError):
        client.wait_for_signature(draft["id"], sleep=lambda s: mock.inject.append({"status": 503, "body": {"detail": "busy"}}))


def test_device_poll_rides_out_a_503(mock):
    c = Client(api_url=mock.url, load_credentials=False)
    code = c.request_device_code()
    mock.inject.append({"status": 503, "body": {"detail": "busy"}, "headers": {"Retry-After": "1"}, "path": "/api/connectors/device/token"})
    mock.device_script = ["approve"]
    sleeps = []
    token = c.wait_for_device_token(code, sleep=sleeps.append)
    assert token["api_key"].startswith("tc_muse_") and sleeps == [5, 1]
