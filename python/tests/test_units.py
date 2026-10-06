import base64
import time
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest

from tourclaim.client import conflict_code, describe_error_body, retry_after_seconds, sentences
from tourclaim.config import normalize_api_url, resolve_api_url, user_agent
from tourclaim.eml import decode_header, parse_eml
from tourclaim.errors import CONFLICT_CODES, APIError, TourClaimError, UsageError
from tourclaim.fields import apply_clears, parse_assignments
from tourclaim.filetype import detect_content_type, format_bytes
from tourclaim.format import format_time, parse_server_time, relative_time
from tourclaim.output import Output

# ---- fields ----


def test_types_key_value_pairs_by_the_schema():
    assert parse_assignments(
        [
            "merchant_name=Example Air",
            "trip_date=2026-03-04",
            "booking_amount=250",
            "refunded_amount=0.00",
            "currency=usd",
            "card_product_id=412",
            "reason_category=Weather",
            "other_insurance=no",
            "medical.provider_seen=No",
            "medical.prior_condition=true",
            "narrative=a=b and c",
        ]
    ) == {
        "merchant_name": "Example Air",
        "trip_date": "2026-03-04",
        "booking_amount": "250",
        "refunded_amount": "0.00",
        "currency": "USD",
        "card_product_id": 412,
        "reason_category": "WEATHER",
        "other_insurance": "NO",
        "medical": {"provider_seen": False, "prior_condition": True},
        "narrative": "a=b and c",
    }


def test_passes_colon_equals_values_through_as_json():
    assert parse_assignments(['card_product_id:=412', 'medical:={"provider_seen":true}', "medical.prior_condition:=null", 'narrative:="x"']) == {
        "card_product_id": 412,
        "medical": {"provider_seen": True, "prior_condition": None},
        "narrative": "x",
    }
    with pytest.raises(UsageError, match="JSON object"):
        parse_assignments(["medical:=[1]"])
    with pytest.raises(UsageError, match='Unknown field "medical.diagnosis"'):
        parse_assignments(['medical:={"diagnosis":"x"}'])
    with pytest.raises(UsageError, match="JSON value"):
        parse_assignments(["card_product_id:=nope"])


def test_clears_with_null_and_catches_conflicts():
    assert apply_clears(["narrative", "medical.provider_details"], {}) == {"narrative": None, "medical": {"provider_details": None}}
    assert apply_clears(["medical"], {}) == {"medical": None}
    with pytest.raises(UsageError, match="both set and cleared"):
        apply_clears(["narrative"], {"narrative": "x"})
    with pytest.raises(UsageError, match="cleared and set"):
        parse_assignments(["medical.provider_seen=true"], {"medical": None})


@pytest.mark.parametrize(
    "pair",
    ["unknown=1", "medical.unknown=1", "medical.provider_seen.x=1", "card_product_id=12a", "booking_amount=1,250.00", "trip_date=2026/03/04",
     "medical.provider_seen=maybe", "currency=EUR", "nonsense", "booking_amount=1.234", "card_product_id=١٢"],
)
def test_rejects_unknown_and_malformed_values(pair):
    with pytest.raises(UsageError):
        parse_assignments([pair])


# ---- filetype ----


def test_detects_pdf_jpeg_and_png_by_their_first_bytes_only():
    assert detect_content_type(b"%PDF-1.7\n") == "application/pdf"
    assert detect_content_type(bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 0])) == "image/jpeg"
    assert detect_content_type(bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0])) == "image/png"
    assert detect_content_type(b" %PDF-1.7") is None
    assert detect_content_type(b"GIF89a") is None
    assert detect_content_type(b"") is None
    assert [format_bytes(n) for n in (68, 2048, 5 * 1024 * 1024 + 1)] == ["68 B", "2.0 KiB", "5.00 MiB"]


# ---- eml ----


def test_reads_headers_decodes_encoded_words_and_the_text_part():
    raw = "\n".join(
        [
            "Subject: =?iso-8859-1?Q?R=E9servation_annul=E9e?=",
            " =?utf-8?B?IOKckw==?=",
            "From: =?utf-8?Q?Caf=C3=A9_Tours?= <ops@cafe.test>",
            "Date: Tue, 3 Mar 2026 09:05:00 -0500",
            "Message-ID: <abc@cafe.test>",
            "Content-Type: multipart/mixed; boundary=outer",
            "",
            "preamble",
            "--outer",
            'Content-Type: multipart/alternative; boundary="inner"',
            "",
            "--inner",
            "Content-Type: text/plain; charset=utf-8",
            "Content-Transfer-Encoding: base64",
            "",
            base64.b64encode("Votre réservation est annulée.\nMerci.".encode()).decode(),
            "--inner",
            "Content-Type: text/html",
            "",
            "<p>no</p>",
            "--inner--",
            "--outer",
            "Content-Type: text/plain",
            "Content-Disposition: attachment; filename=x.txt",
            "",
            "attachment text",
            "--outer--",
        ]
    )
    parsed = parse_eml(raw.encode("utf-8"))
    assert parsed.subject == "Réservation annulée ✓"
    assert parsed.sender == "Café Tours <ops@cafe.test>"
    assert parsed.date == "2026-03-03T14:05:00.000Z"
    assert parsed.message_id == "abc@cafe.test"
    assert parsed.text == "Votre réservation est annulée.\nMerci."


def test_handles_a_single_part_message_raw_utf8_headers_and_no_text_part():
    simple = parse_eml("Subject: Booking café\r\nFrom: a@b.test\r\n\r\nHello\r\nthere\r\n".encode("utf-8"))
    assert simple.subject == "Booking café"
    assert simple.text == "Hello\nthere"
    assert simple.date is None
    assert parse_eml(b"Content-Type: text/html\n\n<p>x</p>").text is None
    assert decode_header("plain") == "plain"


def test_decodes_windows_1252_text_as_the_encoding_standard_says():
    msg = parse_eml(b"From: x@y.test\nContent-Type: text/plain; charset=iso-8859-1\n\nIt\x92s caf\xe9.\n")
    assert msg.text == "It’s café."


# ---- config ----


def test_normalizes_api_urls_and_refuses_unsafe_ones():
    assert normalize_api_url("https://app.getcopernican.com/") == "https://app.getcopernican.com"
    assert normalize_api_url("https://app.getcopernican.com/api/connectors/v1") == "https://app.getcopernican.com"
    assert normalize_api_url("https://staging.example.com/prefix/") == "https://staging.example.com/prefix"
    assert normalize_api_url("http://localhost:4010") == "http://localhost:4010"
    assert normalize_api_url("http://127.0.0.1:4010") == "http://127.0.0.1:4010"
    assert normalize_api_url("http://[::1]:4010/") == "http://[::1]:4010"
    for bad, message in (
        ("http://app.getcopernican.com", "plain http"),
        ("ftp://x", "https"),
        ("https://user:pw@x.test", "user name or password"),
        ("not a url", "valid URL"),
        ("https://x.test:99999", "valid URL"),
    ):
        with pytest.raises(UsageError, match=message):
            normalize_api_url(bad)


def test_prefers_flag_then_environment_then_default():
    assert resolve_api_url(None, {}) == "https://app.getcopernican.com"
    assert resolve_api_url(None, {"TOURCLAIM_API_URL": "https://env.test"}) == "https://env.test"
    assert resolve_api_url("https://flag.test", {"TOURCLAIM_API_URL": "https://env.test"}) == "https://flag.test"
    with pytest.raises(UsageError, match="TOURCLAIM_API_URL"):
        resolve_api_url(None, {"TOURCLAIM_API_URL": "http://insecure.test"})


def test_builds_the_user_agent():
    assert user_agent("darwin", "arm64", "3.12.4") == "tourclaim-py/0.1.1 (darwin arm64; python 3.12.4)"
    assert user_agent().startswith("tourclaim-py/0.1.1 (")


# ---- api helpers ----


def test_reads_retry_after_as_seconds_or_a_date():
    assert retry_after_seconds("7") == 7
    assert retry_after_seconds(None) == 5
    assert retry_after_seconds("soon") == 5
    later = format_datetime(datetime.fromtimestamp(time.time() + 30, timezone.utc), usegmt=True)
    assert 29 <= retry_after_seconds(later) <= 31


def test_describes_every_error_body_shape():
    assert describe_error_body(409, {"detail": "Intake changed"}) == "Intake changed"
    assert (
        describe_error_body(422, {"detail": [{"loc": ["body", "fields", "trip_date"], "type": "date_from_datetime_parsing", "msg": "Invalid field value"}]})
        == "The API rejected these values: fields.trip_date (date_from_datetime_parsing)."
    )
    assert describe_error_body(422, {"detail": []}) == "The API rejected the request as invalid."
    assert describe_error_body(429, {"error": "Rate limit exceeded: 60 per 1 minute"}) == "Rate limit exceeded: 60 per 1 minute"
    assert describe_error_body(400, {"error": "access_denied", "error_description": "Declined"}) == "Declined"
    assert describe_error_body(502, "<html>bad gateway</html>") == "The API answered HTTP 502."
    assert sentences("One", "Two.", None, "Three:") == "One. Two. Three:"


def test_treats_zone_less_server_times_as_utc():
    assert parse_server_time("2026-11-03T18:00:00") == datetime(2026, 11, 3, 18, tzinfo=timezone.utc)
    assert parse_server_time("2026-11-03T18:00:00+02:00") == datetime(2026, 11, 3, 16, tzinfo=timezone.utc)
    assert format_time("2026-11-03T18:00:00") == "2026-11-03 18:00 UTC"
    assert format_time(None) == "unknown"
    assert format_time("garbage") == "garbage"


def test_relative_time_reads_naturally_at_every_scale():
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc).timestamp()
    assert relative_time("2026-10-04T11:59:50Z", now) == "just now"
    assert relative_time("2026-10-04T11:45:00Z", now) == "15 minutes ago"
    assert relative_time("2026-10-04T14:00:00Z", now) == "in 2 hours"
    assert relative_time("2026-11-03T11:59:00", now) == "in 30 days"
    assert relative_time("2026-10-05T12:00:00Z", now) == "in 1 day"
    assert relative_time("2026-10-04T13:30:00Z", now) == "in 2 hours", "rounds half up like Math.round"


# ---- output ----


def test_redacts_anything_that_looks_like_a_key_in_every_stream():
    out, err = [], []
    o = Output(out.append, err.append, False)
    o.add_secret("custom-secret-value")
    o.add_secret("short")
    o.line("key tc_muse_abcDEF123_-xyz and custom-secret-value and short")
    o.info("tc_muse_zzzzzzzz")
    assert o.error(RuntimeError("leak tc_muse_qqqqqqqq")) == 1
    assert "".join(out) == "key tc_muse_[redacted] and [redacted] and short\n"
    assert "zzzz" not in "".join(err) and "qqqq" not in "".join(err)


def test_json_errors_carry_code_message_exit_code_status_extra_and_detail():
    err = []
    o = Output(lambda t: None, err.append, True)
    error = APIError(422, "bad", code="invalid", body={"detail": [{"loc": ["body", "x"]}]}, extra={"idempotency_key": "k"})
    assert o.error(error) == 1
    import json

    assert json.loads("".join(err)) == {
        "error": {"code": "invalid", "message": "bad", "exit_code": 1, "status": 422, "idempotency_key": "k", "detail": [{"loc": ["body", "x"]}]}
    }
    err.clear()
    o.error(TourClaimError("plain"))
    assert json.loads("".join(err)) == {"error": {"code": "error", "message": "plain", "exit_code": 1}}


# ---- conflict codes ----


def test_prefers_the_x_tourclaim_error_header():
    assert conflict_code({"x-tourclaim-error": "duplicate_booking"}, {"detail": "anything"}) == "duplicate_booking"
    assert conflict_code({"x-tourclaim-error": "Not A Code!"}, {"detail": "Intake changed; retry"}) == "stale_revision"


def test_reads_every_server_message_when_the_header_is_absent():
    cases = [
        ("Idempotency key was already used with different fields", "idempotency_key_reused"),
        ("This intake belongs to an earlier connection; use a new idempotency key", "idempotency_key_other_connection"),
        ("Concurrent request; retry with the same idempotency key", "concurrent_request"),
        ("Submitted intake is read-only", "intake_submitted"),
        ("A submitted claim cannot be deleted here. See the data deletion instructions to request deletion.", "intake_submitted"),
        ("Intake changed; retrieve the current revision and try again", "stale_revision"),
        ("Intake changed; retrieve the current state", "stale_revision"),
        ("Evidence source was already imported with different contents", "evidence_conflict"),
        ("Intake is incomplete", "intake_incomplete"),
        ("Complete the remaining questions in Muse first", "intake_incomplete"),
        ("The traveler must approve this exact intake revision", "approval_required"),
        ("Authorization changed; ask the traveler to review again", "approval_outdated"),
        ("Approval expired; ask the traveler to review again", "approval_outdated"),
        ("A claim for this booking already exists", "duplicate_booking"),
        ("Something new", "conflict"),
    ]
    for message, code in cases:
        assert conflict_code({}, {"detail": message}) == code, message
    assert set(CONFLICT_CODES) <= {code for _, code in cases}
