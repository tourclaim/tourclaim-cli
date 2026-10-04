"""Drives the real `python -m tourclaim` in child processes against the mock
API, the way a person or an agent would: sign in, fill a draft, add evidence,
have the traveler sign in "their browser", submit and list claims."""

import json
import os
import re
import stat
import sys
import urllib.request

import pytest
from conftest import IS_WINDOWS, PNG, creds_file, spawn_cli
from mock_server import MockServer


@pytest.fixture(scope="module")
def mock():
    server = MockServer(device_interval=1, enforce_device_interval=True)
    server.start()
    yield server
    server.stop()


def visit(url):
    """The traveler opens a link in their browser."""
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status


def test_login_start_set_attach_add_email_sign_wait_submit_claims_list_logout(mock, home):
    env = {"TOURCLAIM_API_URL": mock.url}
    outputs = []

    def run(argv, stdin=None):
        r = spawn_cli(argv, home=home, env=env, stdin=stdin).done()
        outputs.append(r)
        return r

    # Sign in. The first JSON line is what an agent relays to its human.
    login = spawn_cli(["login", "--json", "--no-browser"], home=home, env=env)
    first = json.loads(login.first_line())
    assert first["event"] == "device_code"
    assert visit(first["verification_uri_complete"]) == 200
    signed_in = login.done()
    outputs.append(signed_in)
    assert signed_in.code == 0, signed_in.stderr
    assert signed_in.json()["event"] == "signed_in"
    assert signed_in.json()["account_email"] == "pat@example.com"

    path = creds_file(home)
    if not IS_WINDOWS:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    key = json.load(open(path, encoding="utf-8"))[mock.url]["api_key"]
    assert key.startswith("tc_muse_")

    status = run(["status"])
    assert status.code == 0, status.stderr
    assert status.stdout.startswith("REVIEW MODE")

    assert run(["cards", "search", "synthetic", "--json"]).json()[0]["id"] == 900

    started = run(["intake", "start", "--json", "--set", "merchant_name=Synthetic Harbor Tours", "--set", "reason_category=weather"])
    assert started.code == 0, started.stderr
    draft = started.json()["id"]
    assert started.json()["state"] == "collecting"
    assert [d["id"] for d in run(["intake", "list", "--json"]).json()] == [draft]

    filled = run(
        [
            "intake", "set", draft,
            "booking_ref=REVIEW-0001",
            "trip_date=2026-10-01",
            "booking_amount=620.00",
            "refunded_amount=0.00",
            "currency=USD",
            "card_product_id=900",
            "narrative=Fictional tour cancelled by a storm warning. No real travel data.",
            "other_insurance=no",
        ]
    )
    assert filled.code == 0, filled.stderr
    assert re.search(r"State:\s+needs_approval", filled.stdout)

    receipt = os.path.join(home, "receipt.png")
    with open(receipt, "wb") as handle:
        handle.write(PNG)
    refused = run(["intake", "attach", draft, receipt, "--type", "receipt"])
    assert refused.code == 2, "attach without --yes and without a terminal must refuse"
    assert mock.requests_to("POST", f"/api/connectors/v1/intakes/{draft}/attachments") == []
    attached = run(["intake", "attach", draft, receipt, "--type", "receipt", "--yes", "--json"])
    assert attached.code == 0, attached.stderr
    assert len(attached.json()["evidence"]) == 1

    eml = os.path.join(home, "cancel.eml")
    with open(eml, "wb") as handle:
        handle.write(
            b"From: ops@harbor.test\nSubject: Tour cancelled\nDate: Wed, 30 Sep 2026 08:00:00 +0000\n\n"
            b"Your tour on 1 October is cancelled due to the storm warning.\n"
        )
    emailed = run(["intake", "add-email", draft, "--eml", eml, "--yes"])
    assert emailed.code == 0, emailed.stderr
    assert "2 evidence items" in emailed.stdout

    # Wait for the signature; the traveler signs in their browser meanwhile.
    sign = spawn_cli(["intake", "sign", draft, "--wait", "--json", "--no-browser"], home=home, env=env)
    waiting = json.loads(sign.first_line())
    assert waiting["event"] == "waiting_for_signature"
    assert visit(waiting["review_url"]) == 200
    signed = sign.done()
    outputs.append(signed)
    assert signed.code == 0, signed.stderr
    assert signed.json()["state"] == "ready_to_submit"

    submitted = run(["intake", "submit", draft, "--json"])
    assert submitted.code == 0, submitted.stderr
    claim = submitted.json()
    assert claim["status"] == "INTAKE_RECEIVED" and claim["mode"] == "review"
    assert [c["id"] for c in run(["claims", "list", "--json"]).json()] == [claim["id"]]

    logout = run(["logout", "--json"])
    assert logout.code == 0, logout.stderr
    assert logout.json()["revoked"] is True
    assert run(["status", "--json"]).code == 3

    for r in outputs:
        assert key not in r.stdout and key not in r.stderr, "the key must never be printed"


def test_reads_a_key_piped_to_login_with_token(mock, tmp_path):
    key = mock.issue_key(traveler="sam@example.com")
    r = spawn_cli(["login", "--with-token"], home=str(tmp_path), env={"TOURCLAIM_API_URL": mock.url}, stdin=f"{key}\n".encode()).done()
    assert r.code == 0, r.stderr
    assert "Signed in as sam@example.com" in r.stdout
    assert key not in r.stdout and key not in r.stderr


def test_prints_help_and_the_version_from_the_module(home):
    help_ = spawn_cli(["--help"], home=home, env={}).done()
    assert help_.code == 0
    assert "Usage: tourclaim <command>" in help_.stdout
    version = spawn_cli(["--version"], home=home, env={}).done()
    assert re.fullmatch(r"\d+\.\d+\.\d+\r?\n", version.stdout)
    assert version.stderr == "", "no warnings on stderr"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX pipe semantics")
def test_a_closed_stdout_is_not_an_error(mock, home):
    import subprocess

    child_env = {k: v for k, v in os.environ.items() if not k.startswith("TOURCLAIM_")}
    child_env.update({"TOURCLAIM_API_URL": mock.url, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "APPDATA": home})
    process = subprocess.Popen([sys.executable, "-m", "tourclaim", "schema"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env)
    process.stdout.read(10)
    process.stdout.close()
    stderr = process.stderr.read().decode()
    process.wait(timeout=30)
    assert "Traceback" not in stderr and "error:" not in stderr
