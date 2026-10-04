import base64
import itertools
import json
import os
import re
import urllib.request

import pytest
from conftest import PDF, PNG, run_cli, save_key
from mock_server import ClaimRecord, MockServer

COMPLETE = [
    "merchant_name=Example Air",
    "booking_ref=EXA-482913",
    "trip_date=2026-03-04",
    "booking_amount=250.00",
    "refunded_amount=50.00",
    "currency=usd",
    "card_product_id=412",
    "reason_category=weather",
    "narrative=The harbor closed for a storm warning and the tour was cancelled.",
    "other_insurance=no",
]
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_travelers = itertools.count(1)


class Session:
    """A traveler per test, with a key as tourclaim login mints it, stored in a fresh home."""

    def __init__(self, mock, home):
        self.mock = mock
        self.home = home
        self.traveler = f"traveler{next(_travelers)}@example.com"
        self.key = mock.issue_key(traveler=self.traveler, channel="cli")
        save_key(home, mock.url, self.key)

    def cli(self, argv, **options):
        """Runs the CLI and checks no key appears in its output."""
        options.setdefault("api_url", self.mock.url)
        r = run_cli(argv, home=self.home, **options)
        for secret in list(self.mock.keys):
            assert secret not in r.stdout, f"key printed on stdout by: {argv}"
            assert secret not in r.stderr, f"key printed on stderr by: {argv}"
        return r

    def start(self, pairs=COMPLETE):
        r = self.cli(["intake", "start", "--json", *[a for p in pairs for a in ("--set", p)]])
        assert r.code == 0, r.stderr
        return r.json()["id"]


@pytest.fixture
def s(mock, home):
    mock.rate_limit = {"remaining": 0, "retry_after": "1"}
    mock.inject = []
    mock.on_request = None
    mock.enabled = True
    return Session(mock, home)


def write(home, name, data):
    path = os.path.join(home, name)
    with open(path, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


# ---- start ----


def test_start_generates_a_uuid_idempotency_key_and_prints_what_is_missing(s, mock):
    r = s.cli(["intake", "start", "--set", "merchant_name=Example Air", "--set", "reason_category=airline_cancellation"])
    assert r.code == 0, r.stderr
    req = mock.requests_to("POST", "/api/connectors/v1/intakes")[-1]
    assert UUID4.fullmatch(req.headers["idempotency-key"])
    assert req.body == {"fields": {"merchant_name": "Example Air", "reason_category": "AIRLINE_CANCELLATION"}}
    assert re.match(r"Started draft [0-9a-f-]{36}", r.stdout)
    assert re.search(r"State:\s+collecting", r.stdout)
    assert re.search(r"Mode:\s+review mode: claims are synthetic and nothing is filed", r.stdout)
    assert re.search(r"Missing \(8\): booking_ref, trip_date", r.stdout)
    assert "1. What is the booking confirmation number?" in r.stdout
    assert "Find it again any time with: tourclaim intake list" in r.stdout


def test_start_prints_the_api_response_unchanged_with_json(s):
    r = s.cli(["intake", "start", "--json"])
    assert r.code == 0, r.stderr
    intake = r.json()
    assert intake["state"] == "collecting" and intake["revision"] == 1
    assert intake["coverage_status"] == "not_determined"
    assert len(r.json_lines()) == 1


def test_start_same_key_same_body_same_draft_different_body_exits_4(s):
    args = ["intake", "start", "--json", "--idempotency-key", "retry-key-0001", "--set", "booking_ref=ABC-1"]
    first, again = s.cli(args), s.cli(args)
    assert first.json()["id"] == again.json()["id"]
    different = s.cli(["intake", "start", "--json", "--idempotency-key", "retry-key-0001", "--set", "booking_ref=ABC-2"])
    assert different.code == 4
    err = different.json_error()
    assert err["code"] == "idempotency_key_reused"
    assert err["idempotency_key"] == "retry-key-0001"
    assert "new --idempotency-key" in err["message"]


def test_start_on_concurrent_request_says_retry_with_the_same_key(s, mock):
    mock.inject.append({"status": 409, "body": {"detail": "Concurrent request; retry with the same idempotency key"}, "headers": {"X-TourClaim-Error": "concurrent_request"}})
    r = s.cli(["intake", "start", "--json", "--idempotency-key", "concurrent-0001"])
    assert r.code == 4
    assert r.json_error()["code"] == "concurrent_request"
    assert "Retry with the same fields and --idempotency-key concurrent-0001" in r.json_error()["message"]
    assert "new --idempotency-key" not in r.json_error()["message"]


def test_start_rejects_a_malformed_idempotency_key_before_calling(s, mock):
    before = len(mock.requests)
    r = s.cli(["intake", "start", "--idempotency-key", "short"])
    assert r.code == 2
    assert len(mock.requests) == before


def test_start_merges_fields_file_with_set_and_rejects_unknown_fields(s, home):
    path = write(home, "fields.json", json.dumps({"merchant_name": "From File", "booking_ref": "F-1", "medical": {"provider_seen": True}}))
    r = s.cli(["intake", "start", "--json", "--fields-file", path, "--set", "booking_ref=F-2"])
    assert r.code == 0, r.stderr
    assert r.json()["fields"]["merchant_name"] == "From File"
    assert r.json()["fields"]["booking_ref"] == "F-2"
    write(home, "fields.json", json.dumps({"card_number": "4111"}))
    bad = s.cli(["intake", "start", "--fields-file", path])
    assert bad.code == 2
    assert 'Unknown field "card_number"' in bad.stderr


def test_start_reads_fields_from_stdin(s):
    r = s.cli(["intake", "start", "--json", "--fields-file", "-"], stdin=b'{"merchant_name": "Piped Tours"}')
    assert r.code == 0, r.stderr
    assert r.json()["fields"]["merchant_name"] == "Piped Tours"


def test_start_says_retry_with_the_same_key_when_the_outcome_is_unknown(s, mock):
    mock.rate_limit = {"remaining": 2, "retry_after": "1"}
    r = s.cli(["intake", "start", "--json", "--idempotency-key", "unknown-outcome-1"])
    assert r.code == 5
    assert "--idempotency-key unknown-outcome-1" in r.json_error()["message"]


# ---- set ----


def test_set_types_values_clears_with_null_and_passes_json_through(s, mock):
    draft = s.start(["merchant_name=Example Air"])
    r = s.cli(
        [
            "intake", "set", draft,
            "card_product_id=412",
            "reason_category=illness",
            "booking_amount=250.00",
            "medical.provider_seen=yes",
            "medical.symptom_onset_date=2026-03-01",
            'other_insurance:="UNSURE"',
            "--clear", "merchant_name",
            "--json",
        ]
    )
    assert r.code == 0, r.stderr
    patch = mock.requests_to("PATCH", f"/api/connectors/v1/intakes/{draft}")[-1]
    assert patch.body == {
        "expected_revision": 1,
        "fields": {
            "card_product_id": 412,
            "reason_category": "ILLNESS",
            "booking_amount": "250.00",
            "medical": {"provider_seen": True, "symptom_onset_date": "2026-03-01"},
            "other_insurance": "UNSURE",
            "merchant_name": None,
        },
    }
    intake = r.json()
    assert intake["revision"] == 2
    assert intake["fields"]["merchant_name"] is None
    assert "medical.prior_condition" in intake["missing_fields"]


def test_set_merges_medical_answers_and_reads_the_revision_first(s, mock):
    draft = s.start(["reason_category=injury", "medical.provider_seen=true"])
    r = s.cli(["intake", "set", draft, "medical.prior_condition=false", "--json"])
    assert r.code == 0, r.stderr
    assert r.json()["fields"]["medical"] == {"provider_seen": True, "prior_condition": False}
    assert len(mock.requests_to("GET", f"/api/connectors/v1/intakes/{draft}")) == 1


def test_set_on_409_rereads_explains_exits_4_and_does_not_retry(s, mock):
    draft = s.start(["merchant_name=Example Air"])

    def bump(req):
        if req.method == "PATCH":
            mock.intakes[draft].revision += 1

    mock.on_request = bump
    r = s.cli(["intake", "set", draft, "booking_ref=X-1", "--json"])
    assert r.code == 4
    err = r.json_error()
    assert err["code"] == "stale_revision"
    assert err["current_revision"] == 2
    assert re.search(r"now at revision 2 .*used revision 1.*Nothing was saved", err["message"])
    assert len(mock.requests_to("PATCH", f"/api/connectors/v1/intakes/{draft}")) == 1


def test_set_rejects_unknown_fields_empty_values_and_empty_changes(s, mock):
    draft = s.start([])
    for argv in (
        ["intake", "set", draft, "card_number=4111111111111111"],
        ["intake", "set", draft, "narrative="],
        ["intake", "set", draft],
        ["intake", "set", draft, "trip_date=March 4"],
        ["intake", "set", draft, "booking_amount=$250"],
        ["intake", "set", draft, "reason_category=bored"],
        ["intake", "set", draft, "medical=yes"],
        ["intake", "set", draft, "merchant_name=A", "--clear", "merchant_name"],
    ):
        r = s.cli(argv)
        assert r.code == 2, f"{argv}: {r.stderr}"
    assert mock.requests_to("PATCH", f"/api/connectors/v1/intakes/{draft}") == []


def test_set_reports_a_422_validation_list_with_each_field(s):
    draft = s.start([])
    human = s.cli(["intake", "set", draft, "narrative=too short"])
    assert human.code == 1
    assert "error: The API rejected these values: fields.narrative (string_too_short)" in human.stderr
    assert "fields.narrative: Invalid field value (string_too_short)" in human.stderr
    as_json = s.cli(["intake", "set", draft, "narrative=too short", "--json"])
    assert as_json.code == 1
    assert as_json.json_error()["detail"] == [{"loc": ["body", "fields", "narrative"], "type": "string_too_short", "msg": "Invalid field value"}]
    assert as_json.json_error()["status"] == 422


def test_set_reports_a_422_string_detail(s):
    draft = s.start(["booking_amount=100.00"])
    r = s.cli(["intake", "set", draft, "refunded_amount=200.00"])
    assert r.code == 1
    assert "refund cannot exceed amount paid" in r.stderr


def test_set_warns_that_a_change_cancels_the_signature(s, mock):
    draft = s.start()
    mock.sign(draft)
    r = s.cli(["intake", "set", draft, "cancellation_policy=Non-refundable within 48 hours."])
    assert r.code == 0, r.stderr
    assert "cancelled the traveler's signature" in r.stderr
    assert re.search(r"State:\s+needs_approval", r.stdout)


# ---- errors ----


def test_404_explains_which_drafts_a_key_can_reach(s, mock):
    draft = s.start([])
    other = mock.issue_key(traveler=s.traveler, channel="key")
    r = s.cli(["intake", "show", draft, "--json"], env={"TOURCLAIM_API_KEY": other})
    assert r.code == 6
    err = r.json_error()
    assert err["code"] == "not_found"
    assert "reachable only with the key that started them" in err["message"]
    assert "tourclaim intake list" in err["message"]


def test_401_exits_3_and_no_key_exits_3_without_a_request(s, mock, tmp_path_factory):
    mock.keys[s.key].revoked = True
    r = s.cli(["claims", "list", "--json"])
    assert r.code == 3
    assert r.json_error()["code"] == "unauthorized"
    assert "tourclaim login" in r.json_error()["message"]
    empty = str(tmp_path_factory.mktemp("empty"))
    before = len(mock.requests)
    r = run_cli(["claims", "list"], home=empty, api_url=mock.url)
    assert r.code == 3
    assert "Not signed in" in r.stderr
    assert len(mock.requests) == before


def test_403_for_a_missing_permission_exits_3(s, mock):
    read_only = mock.issue_key(traveler=s.traveler, scopes=["claims:read"])
    r = s.cli(["cards", "search", "sapphire", "--json"], env={"TOURCLAIM_API_KEY": read_only})
    assert r.code == 3
    assert r.json_error()["code"] == "forbidden"


def test_429_is_retried_once_after_retry_after_then_exits_5(s, mock):
    mock.rate_limit = {"remaining": 1, "retry_after": "2"}
    r = s.cli(["claims", "list", "--json"])
    assert r.code == 0, r.stderr
    assert r.sleeps == [2]
    assert "retrying in 2 seconds" in r.stderr
    mock.rate_limit = {"remaining": 2, "retry_after": "2"}
    r = s.cli(["claims", "list", "--json"])
    assert r.code == 5
    assert r.sleeps == [2]
    err = r.json_error()
    assert err["code"] == "rate_limited" and err["retry_after"] == 2
    assert "Rate limit exceeded" in err["message"]


def test_429_asking_for_more_than_60_seconds_is_not_retried(s, mock):
    mock.rate_limit = {"remaining": 1, "retry_after": "120"}
    r = s.cli(["claims", "list", "--json"])
    assert r.code == 5
    assert r.sleeps == []
    assert r.json_error()["retry_after"] == 120


def test_503_exits_7(s, mock):
    mock.enabled = False
    r = s.cli(["claims", "list"])
    assert r.code == 7
    assert "not enabled" in r.stderr


def test_413_maps_to_too_large(s, mock):
    from tourclaim import Client, PayloadTooLargeError

    client = Client(s.key, mock.url)
    with pytest.raises(PayloadTooLargeError) as caught:
        client.request("POST", "/api/connectors/v1/intakes/x/attachments", body={"pad": "x" * (9 * 1024 * 1024)})
    assert caught.value.status == 413 and caught.value.code == "too_large" and caught.value.exit_code == 1


def test_usage_errors_exit_2(s):
    for argv in (["intake", "show"], ["intake", "frobnicate"], ["claims", "list", "--offset", "-1"], ["claims", "list", "--bogus"], ["claims", "list", "--offset"]):
        r = s.cli(argv)
        assert r.code == 2, argv


def test_refuses_plain_http_to_a_non_local_api_url(s):
    r = s.cli(["status"], api_url="http://api.example.com")
    assert r.code == 2
    assert "plain http" in r.stderr


def test_api_url_flag_overrides_the_environment(s, mock):
    r = s.cli(["claims", "list", "--json", "--api-url", mock.url + "/api/connectors/v1/"], api_url="https://unused.example")
    assert r.code == 0, r.stderr


# ---- attach ----


def test_attach_refuses_without_yes_when_there_is_no_terminal(s, mock, home):
    draft = s.start([])
    path = write(home, "receipt.png", PNG)
    r = s.cli(["intake", "attach", draft, path, "--type", "receipt", "--json"])
    assert r.code == 2
    assert "Pass --yes only after the traveler has agreed" in r.json_error()["message"]
    assert r.json_error()["reason"] == "confirmation_required"
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments") == []


def test_attach_asks_on_a_terminal_and_no_uploads_nothing(s, mock, home):
    draft = s.start([])
    path = write(home, "receipt.png", PNG)
    no = s.cli(["intake", "attach", draft, path, "--type", "receipt"], stdin_is_tty=True, answers=["n"])
    assert no.code == 1
    assert re.search(r"Share receipt\.png \(image/png, 68 B\) with TourClaim as receipt evidence", no.prompts[0])
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments") == []
    yes = s.cli(["intake", "attach", draft, path, "--type", "receipt"], stdin_is_tty=True, answers=["yes"])
    assert yes.code == 0, yes.stderr
    assert len(mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments")) == 1
    assert "1 evidence item)" in yes.stdout


def test_attach_uploads_with_yes_detecting_the_type_from_the_bytes(s, mock, home):
    draft = s.start([])
    path = write(home, "itinerary.dat", PDF)
    r = s.cli(["intake", "attach", draft, path, "--type", "itinerary", "--yes", "--json"])
    assert r.code == 0, r.stderr
    body = mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments")[-1].body
    assert body["content_type"] == "application/pdf"
    assert body["filename"] == "itinerary.dat"
    assert body["doc_type"] == "itinerary"
    assert body["user_authorized_sharing"] is True
    assert body["expected_revision"] == 1
    assert base64.b64decode(body["content_base64"]) == PDF
    assert r.json()["evidence"][0]["label"] == "File evidence 1"
    assert r.json()["revision"] == 2


def test_attach_rejects_unsupported_oversized_and_missing_files_and_bad_type(s, mock, home):
    draft = s.start([])
    text = write(home, "notes.pdf", "just text pretending to be a pdf")
    r = s.cli(["intake", "attach", draft, text, "--type", "other", "--yes"])
    assert r.code == 1 and "not a PDF, JPEG or PNG" in r.stderr
    big = write(home, "big.png", PNG + b"\0" * (5 * 1024 * 1024))
    r = s.cli(["intake", "attach", draft, big, "--type", "receipt", "--yes", "--json"])
    assert r.code == 1 and "the limit is 5 MiB" in r.json_error()["message"]
    assert r.json_error()["code"] == "too_large"
    r = s.cli(["intake", "attach", draft, os.path.join(home, "missing.png"), "--type", "receipt", "--yes"])
    assert r.code == 1 and "no such file" in r.stderr
    r = s.cli(["intake", "attach", draft, home, "--type", "receipt", "--yes"])
    assert r.code == 1 and "is not a file" in r.stderr
    empty = write(home, "empty.png", b"")
    assert s.cli(["intake", "attach", draft, empty, "--type", "receipt", "--yes"]).code == 1
    assert s.cli(["intake", "attach", draft, text, "--yes"]).code == 2
    assert s.cli(["intake", "attach", draft, text, "--type", "photo", "--yes"]).code == 2
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments") == []


# ---- add-email ----

EML = "\r\n".join(
    [
        'From: "Example Air" <bookings@example-air.test>',
        "To: pat@example.com",
        "Subject: =?UTF-8?B?WW91ciBib29raW5nIEVYQS00ODI5MTMgaGFzIGJlZW4gY2FuY2VsbGVk?=",
        "Date: Tue, 3 Mar 2026 14:05:00 +0000",
        "Message-ID: <18e4c2a9f1b7d035@example-air.test>",
        "MIME-Version: 1.0",
        'Content-Type: multipart/alternative; boundary="b1"',
        "",
        "--b1",
        "Content-Type: text/plain; charset=utf-8",
        "Content-Transfer-Encoding: quoted-printable",
        "",
        "We're sorry: flight EX 204 on 4 March has been cancelled. Caf=C3=A9 vouchers =",
        "are available.",
        "--b1",
        "Content-Type: text/html",
        "",
        "<p>ignored</p>",
        "--b1--",
        "",
    ]
)


def test_add_email_parses_an_eml_and_sends_the_message_unmodified(s, mock, home):
    draft = s.start([])
    path = write(home, "cancel.eml", EML)
    r = s.cli(["intake", "add-email", draft, "--eml", path, "--yes", "--json"])
    assert r.code == 0, r.stderr
    body = mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/email-evidence")[-1].body
    assert body == {
        "expected_revision": 1,
        "provider": "user",
        "message_id": "18e4c2a9f1b7d035@example-air.test",
        "subject": "Your booking EXA-482913 has been cancelled",
        "sender": '"Example Air" <bookings@example-air.test>',
        "sent_at": "2026-03-03T14:05:00.000Z",
        "text": "We're sorry: flight EX 204 on 4 March has been cancelled. Café vouchers are available.",
        "user_authorized_sharing": True,
    }
    assert r.json()["evidence"][0]["label"] == "Email evidence 1"


def test_add_email_reads_an_eml_from_stdin(s, mock):
    draft = s.start([])
    r = s.cli(["intake", "add-email", draft, "--eml", "-", "--yes", "--json"], stdin=EML.encode("utf-8"))
    assert r.code == 0, r.stderr
    assert r.json()["evidence"][0]["kind"] == "email"


def test_add_email_needs_subject_and_from_with_text_file_and_a_consent(s, mock, home):
    draft = s.start([])
    path = write(home, "notice.txt", "Your tour on 4 March was cancelled because of the storm.")
    assert s.cli(["intake", "add-email", draft, "--text-file", path, "--yes"]).code == 2
    r = s.cli(["intake", "add-email", draft, "--text-file", path, "--subject", "Cancelled", "--from", "ops@tours.test"])
    assert r.code == 2
    assert "seen which message will be shared" in r.stderr
    r = s.cli(
        ["intake", "add-email", draft, "--text-file", path, "--subject", "Cancelled", "--from", "ops@tours.test", "--sent-at", "2026-03-03 14:05", "--yes"]
    )
    assert r.code == 2
    assert s.cli(["intake", "add-email", draft, "--yes"]).code == 2
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/email-evidence") == []


def test_add_email_derives_a_stable_message_id_so_adding_twice_changes_nothing(s, mock, home):
    draft = s.start([])
    path = write(home, "notice.txt", "Your tour on 4 March was cancelled because of the storm.\r\nSorry.")
    args = ["intake", "add-email", draft, "--text-file", path, "--subject", "Cancelled", "--from", "ops@tours.test", "--sent-at", "2026-03-03T09:05:00-05:00", "--yes", "--json"]
    first, second = s.cli(args), s.cli(args)
    assert first.code == 0, first.stderr
    assert second.code == 0, second.stderr
    assert second.json()["revision"] == first.json()["revision"]
    assert len(second.json()["evidence"]) == 1
    bodies = [r.body for r in mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/email-evidence")]
    assert bodies[0]["message_id"] == bodies[1]["message_id"]
    assert re.fullmatch(r"sha256-[0-9a-f]{40}", bodies[0]["message_id"])
    assert bodies[0]["sent_at"] == "2026-03-03T14:05:00.000Z"
    assert bodies[0]["text"] == "Your tour on 4 March was cancelled because of the storm.\r\nSorry.", "sent unmodified"


def test_add_email_derived_id_matches_the_node_edition():
    from tourclaim import derive_message_id

    # createHash("sha256").update("ops@tours.test\nCancelled\n\nhello").digest("hex").slice(0, 40), run in Node.
    assert derive_message_id("ops@tours.test", "Cancelled", None, "hello") == "sha256-6451cdea7a64e93d06a06e729d77d894cf775751"


# ---- sign ----


def test_sign_explains_what_is_missing_when_incomplete(s):
    draft = s.start(["merchant_name=Example Air"])
    r = s.cli(["intake", "sign", draft, "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "intake_incomplete"
    assert "booking_ref" in r.json_error()["missing_fields"]


def test_sign_prints_the_review_link_and_opens_it_only_on_a_terminal(s, mock):
    draft = s.start()
    link = f"{mock.url}/connect/muse?intake={draft}"
    r = s.cli(["intake", "sign", draft])
    assert r.code == 0, r.stderr
    assert "Only the traveler can sign" in r.stdout
    assert f"Review and sign: {link}" in r.stdout
    assert r.opened == []
    assert s.cli(["intake", "sign", draft], stdout_is_tty=True).opened == [link]
    assert s.cli(["intake", "sign", draft, "--no-browser"], stdout_is_tty=True).opened == []


def test_sign_wait_polls_every_5_seconds_until_the_traveler_signs(s, mock):
    draft = s.start()
    polls = {"n": 0}

    def on_sleep(_):
        polls["n"] += 1
        if polls["n"] == 2:
            mock.sign(draft)

    r = s.cli(["intake", "sign", draft, "--wait", "--json"], on_sleep=on_sleep)
    assert r.code == 0, r.stderr
    assert r.sleeps == [5, 5]
    waiting, final = r.json_lines()
    assert waiting == {"event": "waiting_for_signature", "intake_id": draft, "review_url": f"{mock.url}/connect/muse?intake={draft}", "timeout_seconds": 900}
    assert final["state"] == "ready_to_submit"


def test_sign_wait_rides_out_a_temporary_503(s, mock):
    draft = s.start()
    count = {"n": 0}

    def on_sleep(_seconds):
        count["n"] += 1
        if count["n"] == 1:
            mock.inject.append({"status": 503, "body": {"detail": "Service busy"}, "headers": {"Retry-After": "1"}})
        if count["n"] == 2:
            mock.sign(draft)

    r = s.cli(["intake", "sign", draft, "--wait", "--json"], on_sleep=on_sleep)
    assert r.code == 0, r.stderr
    assert r.sleeps == [5, 1]
    assert r.json()["state"] == "ready_to_submit"


def test_sign_wait_still_ends_after_repeated_temporary_failures(s, mock):
    draft = s.start()
    r = s.cli(
        ["intake", "sign", draft, "--wait", "--json"],
        on_sleep=lambda _seconds: mock.inject.append({"status": 504, "body": ""}),
    )
    assert r.code == 1
    assert r.json_error()["status"] == 504
    assert r.sleeps == [5, 5, 5]


def test_sign_wait_gives_up_at_timeout(s):
    draft = s.start()
    r = s.cli(["intake", "sign", draft, "--wait", "--timeout", "12", "--json"])
    assert r.code == 1
    assert r.sleeps == [5, 5]
    assert r.json_error()["code"] == "timeout"


def test_sign_says_so_when_already_signed(s, mock):
    draft = s.start()
    mock.sign(draft)
    r = s.cli(["intake", "sign", draft])
    assert r.code == 0
    assert "already signed" in r.stdout
    assert f"tourclaim intake submit {draft}" in r.stdout


# ---- submit and claims ----


def test_submit_refuses_an_unsigned_draft_with_exit_4_and_the_review_link(s, mock):
    draft = s.start()
    r = s.cli(["intake", "submit", draft, "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "approval_required"
    assert r.json_error()["review_url"] == f"{mock.url}/connect/muse?intake={draft}"
    assert mock.intakes[draft].claim_id is None


def test_submit_refuses_an_incomplete_draft_locally_naming_what_is_missing(s, mock):
    draft = s.start(["merchant_name=Example Air"])
    r = s.cli(["intake", "submit", draft, "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "intake_incomplete"
    assert "booking_ref" in r.json_error()["missing_fields"]
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/submit") == []


def test_submit_a_signed_draft_retry_returns_the_same_claim(s, mock):
    draft = s.start()
    mock.sign(draft)
    r = s.cli(["intake", "submit", draft])
    assert r.code == 0, r.stderr
    assert re.search(r"Submitted draft .* as claim [0-9a-f-]{36}\.", r.stdout)
    assert re.search(r"Status:\s+INTAKE_RECEIVED", r.stdout)
    assert "review mode: claims are synthetic and nothing is filed" in r.stdout
    assert "Nothing here decides coverage or promises reimbursement" in r.stdout
    again = s.cli(["intake", "submit", draft, "--json"])
    assert again.code == 0, again.stderr
    claim_id = again.json()["id"]
    assert mock.intakes[draft].claim_id == claim_id
    assert [c["id"] for c in s.cli(["claims", "list", "--json"]).json()] == [claim_id]
    assert s.cli(["claims", "show", claim_id, "--json"]).json()["status"] == "INTAKE_RECEIVED"
    human = s.cli(["claims", "show", claim_id])
    assert re.search(r"Next:\s+Synthetic review claim received", human.stdout)


def test_submit_exits_4_when_the_booking_already_has_a_claim(s, mock):
    first = s.start()
    mock.sign(first)
    assert s.cli(["intake", "submit", first]).code == 0
    second = s.start(COMPLETE[:-1] + ["other_insurance=unsure"])
    mock.sign(second)
    r = s.cli(["intake", "submit", second, "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "duplicate_booking"
    assert "already exists" in r.json_error()["message"]


def test_claims_list_pages_and_points_at_the_next_page(s, mock):
    for i in range(31):
        mock.claims[f"claim-{i}"] = ClaimRecord(
            f"claim-{i}", f"intake-{i}", s.traveler, f"k{i}", "INTAKE_RECEIVED", "Copernican is reviewing your claim and evidence.", "2026-03-05T17:20:11Z", 1000 + i
        )
    try:
        page = s.cli(["claims", "list"])
        assert "More claims may exist: tourclaim claims list --offset 30" in page.stdout
        assert "review mode: claims are synthetic" in page.stdout
        following = s.cli(["claims", "list", "--offset", "30", "--json"])
        assert len(following.json()) == 1
        assert mock.requests_to("GET", "/api/connectors/v1/claims")[-1].q("offset") == "30"
        assert s.cli(["claims", "list", "--offset", "40"]).stdout.strip() == "No more claims."
    finally:
        for i in range(31):
            mock.claims.pop(f"claim-{i}", None)


def test_claims_list_when_there_are_none(s):
    assert s.cli(["claims", "list"]).stdout.strip() == "No submitted claims yet."


def test_claims_show_exits_6_for_an_unknown_claim(s):
    r = s.cli(["claims", "show", "nope"])
    assert r.code == 6
    assert "No claim nope for this traveler" in r.stderr


# ---- list ----


def test_list_shows_open_drafts_most_recently_changed_first(s, mock):
    first = s.start(["merchant_name=First Tours"])
    second = s.start(["merchant_name=Second Air", "booking_ref=SA-1"])
    submitted = s.start()
    mock.sign(submitted)
    assert s.cli(["intake", "submit", submitted]).code == 0
    assert s.cli(["intake", "set", first, "trip_date=2026-03-04"]).code == 0

    as_json = s.cli(["intake", "list", "--json"])
    assert as_json.code == 0, as_json.stderr
    assert [d["id"] for d in as_json.json()] == [first, second]
    assert as_json.json()[0]["fields"]["trip_date"] == "2026-03-04"

    human = s.cli(["intake", "list"])
    assert re.match(r"DRAFT\s+STATE\s+MERCHANT\s+BOOKING\s+MISSING", human.stdout)
    assert re.search(rf"{first}\s+collecting\s+First Tours\s+-\s+8", human.stdout)
    assert re.search(rf"{second}\s+collecting\s+Second Air\s+SA-1\s+8", human.stdout)
    assert submitted not in human.stdout


def test_list_says_when_there_are_none(s):
    r = s.cli(["intake", "list"])
    assert r.code == 0
    assert "No open drafts. Start one with: tourclaim intake start" in r.stdout
    assert s.cli(["intake", "list", "--json"]).json() == []


def test_a_new_login_key_reaches_drafts_from_earlier_login_keys_even_revoked(s, mock):
    draft = s.start(["merchant_name=Before Relogin"])
    mock.keys[s.key].revoked = True
    following = mock.issue_key(traveler=s.traveler, channel="cli")
    listed = s.cli(["intake", "list", "--json"], env={"TOURCLAIM_API_KEY": following})
    assert listed.code == 0, listed.stderr
    assert [d["id"] for d in listed.json()] == [draft]
    assert s.cli(["intake", "set", draft, "booking_ref=AFTER-1", "--json"], env={"TOURCLAIM_API_KEY": following}).code == 0


def test_drafts_from_other_apps_keys_and_cli_drafts_do_not_see_each_other(s, mock):
    cli_draft = s.start(["merchant_name=From CLI"])
    other = mock.issue_key(traveler=s.traveler, channel="key")
    other_draft = s.cli(["intake", "start", "--json", "--set", "merchant_name=From Muse"], env={"TOURCLAIM_API_KEY": other}).json()["id"]
    assert [d["id"] for d in s.cli(["intake", "list", "--json"], env={"TOURCLAIM_API_KEY": other}).json()] == [other_draft]
    assert [d["id"] for d in s.cli(["intake", "list", "--json"]).json()] == [cli_draft]
    assert s.cli(["intake", "show", other_draft]).code == 6
    stranger = mock.issue_key(traveler="someone-else@example.com", channel="cli")
    assert s.cli(["intake", "list", "--json"], env={"TOURCLAIM_API_KEY": stranger}).json() == []


def test_list_pages_30_at_a_time(s, mock):
    for i in range(31):
        s.start([f"booking_ref=PAGE-{i}"])
    page = s.cli(["intake", "list"])
    assert "More drafts may exist: tourclaim intake list --offset 30" in page.stdout
    following = s.cli(["intake", "list", "--offset", "30", "--json"])
    assert len(following.json()) == 1
    assert mock.requests_to("GET", "/api/connectors/v1/intakes")[-1].q("offset") == "30"


# ---- 409 causes ----


def test_uses_the_x_tourclaim_error_code_as_the_json_error_code(s, mock):
    draft = s.start()
    r = s.cli(["intake", "set", draft, "booking_ref=X-2", "--revision", "9", "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "stale_revision"
    assert r.json_error()["current_revision"] == 1
    r = s.cli(["intake", "submit", draft, "--revision", "9", "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "stale_revision"
    assert "it is now at revision 1" in r.json_error()["message"]
    r = s.cli(["intake", "submit", draft, "--revision", "1", "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "approval_required"
    assert "has not signed revision 1" in r.json_error()["message"]
    assert r.json_error()["review_url"] == f"{mock.url}/connect/muse?intake={draft}"


def test_evidence_conflict_the_same_file_again_under_another_type(s, home):
    draft = s.start([])
    path = write(home, "receipt.png", PNG)
    assert s.cli(["intake", "attach", draft, path, "--type", "receipt", "--yes"]).code == 0
    r = s.cli(["intake", "attach", draft, path, "--type", "other", "--yes", "--json"])
    assert r.code == 4
    assert r.json_error()["code"] == "evidence_conflict"
    assert "already has that evidence with different details" in r.json_error()["message"]


def test_approval_outdated_asks_the_traveler_to_sign_again(s, mock):
    draft = s.start()
    mock.sign(draft)
    mock.intakes[draft].approval_outdated = True
    # The draft now reads as needs_approval; only the API can say the signature is out of date.
    r = s.cli(["intake", "submit", draft, "--json"])
    assert r.code == 4
    err = r.json_error()
    assert err["code"] == "approval_outdated"
    assert "signature is out of date" in err["message"]
    assert re.search(r"in their own browser, at: http://127\.0\.0\.1:\d+/connect/muse\?intake=", err["message"])
    assert err["message"].endswith(f"tourclaim intake sign {draft} --wait")


def test_submitting_a_submitted_drafts_set_is_intake_submitted(s, mock):
    draft = s.start()
    mock.sign(draft)
    assert s.cli(["intake", "submit", draft]).code == 0
    r = s.cli(["intake", "set", draft, "booking_ref=LATE", "--json"])
    assert r.code == 4 and r.json_error()["code"] == "intake_submitted"
    r = s.cli(["intake", "set", draft, "booking_ref=LATE", "--revision", "1", "--json"])
    assert r.code == 4 and r.json_error()["code"] == "intake_submitted"


def test_falls_back_to_the_message_when_the_header_is_missing(s, home):
    old = MockServer(no_conflict_header=True)
    old.start()
    try:
        key = old.issue_key(traveler=s.traveler, channel="cli")

        def run_old(argv):
            return run_cli(argv, home=home, api_url=old.url, env={"TOURCLAIM_API_KEY": key})

        run_old(["intake", "start", "--json", "--idempotency-key", "fallback-0001", "--set", "booking_ref=A"])
        r = run_old(["intake", "start", "--json", "--idempotency-key", "fallback-0001", "--set", "booking_ref=B"])
        assert r.json_error()["code"] == "idempotency_key_reused"
        assert "new --idempotency-key" in r.json_error()["message"]
        draft = run_old(["intake", "start", "--json", *[a for p in COMPLETE for a in ("--set", p)]]).json()["id"]
        r = run_old(["intake", "set", draft, "booking_ref=Z", "--revision", "5", "--json"])
        assert r.json_error()["code"] == "stale_revision"
        old.sign(draft)
        assert run_old(["intake", "submit", draft]).code == 0
        r = run_old(["intake", "delete", draft, "--yes", "--json"])
        assert r.code == 4
        assert r.json_error()["code"] == "intake_submitted"
    finally:
        old.stop()


# ---- delete ----


def test_delete_needs_confirmation_then_deletes_a_submitted_draft_exits_4(s, mock):
    draft = s.start([])
    r = s.cli(["intake", "delete", draft])
    assert r.code == 2
    assert draft in mock.intakes
    r = s.cli(["intake", "delete", draft, "--yes", "--json"])
    assert r.code == 0, r.stderr
    assert r.json() == {"id": draft, "deleted": True}
    assert draft not in mock.intakes

    submitted = s.start()
    mock.sign(submitted)
    s.cli(["intake", "submit", submitted])
    r = s.cli(["intake", "delete", submitted, "-y"])
    assert r.code == 4
    assert "muse/data-deletion" in r.stderr


def test_delete_asks_on_a_terminal(s, mock):
    draft = s.start([])
    r = s.cli(["intake", "delete", draft], stdin_is_tty=True, answers=["y"])
    assert r.code == 0
    assert "cannot be undone" in r.prompts[0]
    assert draft not in mock.intakes


# ---- cards and schema ----


def test_cards_search_and_refuses_card_numbers(s, mock):
    r = s.cli(["cards", "search", "sapphire", "--json"])
    assert r.code == 0, r.stderr
    assert [c["id"] for c in r.json()] == [412, 413]
    assert mock.requests_to("GET", "/api/connectors/v1/cards")[-1].q("q") == "sapphire"
    human = s.cli(["cards", "search", "venture", "x"])
    assert re.search(r"501\s+Capital One\s+Venture X", human.stdout)
    assert "does not mean it covers the loss" in human.stdout
    assert s.cli(["cards", "search", "4111", "1111", "1111", "1111"]).code == 2
    assert s.cli(["cards", "search", "x" * 101]).code == 2
    assert 'No cards matched "zzz"' in s.cli(["cards", "search", "zzz"]).stdout


def test_schema_prints_the_full_cli_schema_without_a_key(mock, tmp_path_factory):
    empty = str(tmp_path_factory.mktemp("schema"))
    before = len(mock.requests)
    r = run_cli(["schema", "--json"], home=empty, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.json()["openapi"] == "3.1.0"
    assert r.json()["info"]["title"] == "TourClaim by Copernican"
    assert "get" in r.json()["paths"]["/api/connectors/v1/intakes"], "the drafts list is in the CLI schema"
    assert "/api/connectors/v1/key" in r.json()["paths"], "the key endpoints are in the CLI schema"
    assert [q.path for q in mock.requests[before:]] == ["/api/connectors/v1/openapi-cli.json"]
    assert "authorization" not in mock.requests[-1].headers
    human = run_cli(["schema"], home=empty, api_url=mock.url)
    assert human.stdout.startswith("{\n  ")


def test_schema_falls_back_to_openapi_json_on_a_server_without_openapi_cli_json(tmp_path_factory):
    old = MockServer(legacy_schema=True)
    old.start()
    try:
        r = run_cli(["schema"], home=str(tmp_path_factory.mktemp("legacy")), api_url=old.url)
        assert r.code == 0, r.stderr
        assert json.loads(r.stdout)["info"]["title"] == "TourClaim by Copernican"
        assert [q.path for q in old.requests] == ["/api/connectors/v1/openapi-cli.json", "/api/connectors/v1/openapi.json"]
    finally:
        old.stop()


def test_the_assistant_schema_leaves_out_the_cli_only_operations(mock):
    with urllib.request.urlopen(f"{mock.url}/api/connectors/v1/openapi.json", timeout=10) as response:
        assistant = json.loads(response.read().decode("utf-8"))
    assert "get" not in assistant["paths"]["/api/connectors/v1/intakes"]
    assert "/api/connectors/v1/key" not in assistant["paths"]
    assert sum(len(ops) for ops in assistant["paths"].values()) == 10
