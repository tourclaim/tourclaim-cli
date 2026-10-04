import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone

import pytest
from conftest import IS_WINDOWS, creds_file, run_cli, save_key, store_for
from mock_server import MockServer

from tourclaim.credentials import credentials_path

UA = re.compile(r"^tourclaim-py/0\.1\.0 \(\w+ x86_64; python 3\.12\.0\)$")


@pytest.fixture(autouse=True)
def reset(mock):
    mock.device_script = []
    mock.rate_limit = {"remaining": 0, "retry_after": "1"}
    mock.on_request = None
    mock.enabled = True
    yield


def mode_bits(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ---- login (device flow) ----


@pytest.fixture(scope="module")
def mock():
    server = MockServer(device_interval=5)
    server.start()
    yield server
    server.stop()


def test_polls_until_approved_honoring_interval_and_slow_down_and_saves_0600(mock, home):
    mock.device_script = ["pending", "slow_down", "pending", "approve"]
    r = run_cli(["login", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    # 5 s, 5 s, then slow_down adds 5 s for every later poll.
    assert r.sleeps == [5, 5, 10, 10]
    assert re.search(r"open this page:\n\n  http://127\.0\.0\.1:\d+/connect/cli\n", r.stdout)
    assert re.search(r"^Enter this code on that page: [A-Z]{4}-[A-Z]{4}$", r.stdout, re.M)
    assert "?code=" not in r.stdout, "the code is never put in a link"
    assert re.search(
        r"Signed in as pat@example\.com \(key expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC, in (29|30) days\)", r.stdout
    )
    assert "Review mode: claims are synthetic and nothing is filed" in r.stdout
    assert r.opened == [], "--no-browser must not open anything"

    path = creds_file(home)
    entry = json.load(open(path, encoding="utf-8"))[mock.url]
    assert re.fullmatch(r"tc_muse_[A-Za-z0-9_-]{48}", entry["api_key"])
    assert isinstance(entry["expires_at"], str) and entry["expires_at"].endswith("Z")
    assert isinstance(entry["grant_id"], str)
    assert entry["scopes"] == ["intakes:write", "evidence:write", "claims:submit", "claims:read"]
    if not IS_WINDOWS:
        assert mode_bits(path) == 0o600
        assert mode_bits(os.path.dirname(path)) == 0o700
    assert entry["api_key"] not in r.stdout and entry["api_key"] not in r.stderr, "the key must never be printed"


def test_sends_client_string_scopes_and_user_agent(mock, home):
    mock.device_script = ["approve"]
    before = len(mock.requests)
    r = run_cli(["login", "--no-browser", "--scope", "intakes:write,claims:read"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    body = mock.requests_to("POST", "/api/connectors/device/code")[-1].body
    assert UA.match(body["client"]), body
    assert body["scopes"] == ["intakes:write", "claims:read"]
    for req in mock.requests[before:]:
        assert UA.match(req.headers["user-agent"]), req.headers["user-agent"]


def test_json_mode_prints_code_first_then_result_and_never_the_device_code(mock, home):
    mock.device_script = ["pending", "approve"]
    r = run_cli(["login", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    lines = r.json_lines()
    assert len(lines) == 2
    first, last = lines
    assert first["event"] == "device_code"
    assert re.fullmatch(r"[A-Z]{4}-[A-Z]{4}", first["user_code"])
    assert first["verification_uri"] == f"{mock.url}/connect/cli"
    assert sorted(first) == ["event", "expires_in", "interval", "user_code", "verification_uri"]
    assert first["expires_in"] == 600
    assert "device_code" not in first
    device = mock.device(first["user_code"])
    assert device and device.device_code not in r.stdout
    assert last["event"] == "signed_in"
    assert last["account_email"] == "pat@example.com"
    assert last["mode"] == "review"
    assert last["already_signed_in"] is False
    assert last["key"]["id"] and last["key"]["expires_at"] and isinstance(last["key"]["scopes"], list)
    assert "tc_muse_" not in json.dumps(last)


def test_opens_the_browser_only_when_stdout_is_a_terminal(mock, home, tmp_path_factory):
    mock.device_script = ["approve"]
    r = run_cli(["login"], home=home, api_url=mock.url, stdout_is_tty=True)
    assert r.code == 0, r.stderr
    assert r.opened == [f"{mock.url}/connect/cli"]
    mock.device_script = ["approve"]
    other = str(tmp_path_factory.mktemp("other"))
    r = run_cli(["login"], home=other, api_url=mock.url, stdout_is_tty=False)
    assert r.opened == []


def test_exits_3_when_the_traveler_declines(mock, home):
    mock.device_script = ["pending", "denied"]
    r = run_cli(["login", "--json"], home=home, api_url=mock.url)
    assert r.code == 3
    assert r.json_error()["code"] == "access_denied"
    assert "declined" in r.json_error()["message"]
    assert store_for(home).get(mock.url) is None


def test_exits_3_when_the_code_expired_or_was_used(mock, home):
    mock.device_script = ["expired"]
    r = run_cli(["login"], home=home, api_url=mock.url)
    assert r.code == 3
    assert "expired or was already used" in r.stderr


def test_stops_at_expires_in_after_one_final_poll_at_the_deadline(home):
    short = MockServer(device_interval=5, device_expires_in=12)
    short.start()
    try:
        r = run_cli(["login"], home=home, api_url=short.url)
        assert r.code == 3
        # Never sleeps past the deadline; the poll at the deadline is the last one.
        assert r.sleeps == [5, 5, 2]
        assert len(short.requests_to("POST", "/api/connectors/device/token")) == 3
        assert "expired before it was approved" in r.stderr
    finally:
        short.stop()


def test_collects_an_approval_made_in_the_last_seconds(home):
    short = MockServer(device_interval=5, device_expires_in=12)
    short.start()
    try:
        short.device_script = ["pending", "pending", "approve"]
        r = run_cli(["login", "--no-browser"], home=home, api_url=short.url)
        assert r.code == 0, r.stderr
        assert r.sleeps == [5, 5, 2]
    finally:
        short.stop()


TOKEN = "/api/connectors/device/token"
BUSY = {"status": 503, "body": {"detail": "Service busy"}, "headers": {"Retry-After": "1"}, "path": TOKEN}


def test_a_503_from_the_token_poll_is_temporary_and_the_key_still_signs_in(mock, home):
    mock.inject.append(dict(BUSY))
    mock.device_script = ["approve"]
    r = run_cli(["login", "--no-browser", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.sleeps == [5, 1]
    assert r.json()["event"] == "signed_in"


def test_a_502_or_504_waits_the_interval_and_retry_after_never_stretches_it(mock, home):
    mock.inject.append({"status": 502, "body": "Bad Gateway", "path": TOKEN})
    mock.inject.append({"status": 503, "body": {"detail": "Service busy"}, "headers": {"Retry-After": "30"}, "path": TOKEN})
    mock.device_script = ["approve"]
    r = run_cli(["login", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.sleeps == [5, 5, 5]


def test_temporary_failures_past_the_limit_still_end_the_sign_in(mock, home):
    mock.inject.extend(dict(BUSY) for _ in range(3))
    r = run_cli(["login", "--no-browser", "--json"], home=home, api_url=mock.url)
    assert r.code == 7
    assert r.json_error()["code"] == "unavailable"
    assert r.json_error()["retry_after"] == 1
    assert r.sleeps == [5, 1, 1]


def test_a_later_answer_counts_temporary_failures_from_zero(mock, home):
    mock.inject.extend([dict(BUSY), {"status": 504, "body": "", "path": TOKEN}])
    mock.device_script = ["pending", "approve"]
    polls = {"n": 0}

    def on_request(req):
        if req.path == TOKEN:
            polls["n"] += 1
            if polls["n"] == 4:
                mock.inject.extend([{"status": 502, "body": "", "path": TOKEN}, {"status": 503, "body": {"detail": "busy"}, "path": TOKEN}])

    mock.on_request = on_request
    r = run_cli(["login", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    # 503 (1 s), 504 (interval), pending resets the count, 502 and 503 (interval each), then the key.
    assert r.sleeps == [5, 1, 5, 5, 5, 5]


def test_the_poll_at_the_deadline_is_the_last_one_even_when_it_meets_a_503(home):
    short = MockServer(device_interval=5, device_expires_in=12)
    short.start()
    polls = {"n": 0}

    def on_request(req):
        if req.path == TOKEN:
            polls["n"] += 1
            if polls["n"] == 3:
                short.inject.append({"status": 503, "body": {"detail": "Service busy"}, "path": TOKEN})

    short.on_request = on_request
    try:
        r = run_cli(["login", "--no-browser"], home=home, api_url=short.url)
        assert r.code == 7
        assert r.sleeps == [5, 5, 2]
        assert len(short.requests_to("POST", TOKEN)) == 3
    finally:
        short.stop()


def test_sends_no_key_with_the_polls_when_none_is_stored(mock, home):
    mock.device_script = ["pending", "approve"]
    before = len(mock.requests)
    r = run_cli(["login", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    polls = [q for q in mock.requests[before:] if q.path == "/api/connectors/device/token"]
    assert len(polls) == 2 and all("authorization" not in q.headers for q in polls)


def test_treats_a_429_while_polling_like_slow_down(mock, home):
    polls = {"n": 0}

    def on_request(req):
        if req.path == "/api/connectors/device/token":
            polls["n"] += 1
            if polls["n"] == 1:
                mock.rate_limit = {"remaining": 1, "retry_after": "1"}
            if polls["n"] == 3:
                mock.device_script = ["approve"]

    mock.on_request = on_request
    r = run_cli(["login", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    # The 429 adds 5 s, like slow_down.
    assert r.sleeps == [5, 10, 10]


def test_ignores_verification_uri_complete_from_older_servers(home):
    old = MockServer(device_interval=5, send_complete_uri=True)
    old.start()
    try:
        old.device_script = ["approve"]
        r = run_cli(["login", "--json"], home=home, api_url=old.url, stdout_is_tty=True)
        assert r.code == 0, r.stderr
        assert "verification_uri_complete" not in r.json_lines()[0]
        assert r.opened == [f"{old.url}/connect/cli"]
        assert "?code=" not in r.stdout
    finally:
        old.stop()


def test_at_the_key_limit_the_server_retires_the_oldest_login_key_and_force_does_not_warn(mock, home):
    traveler = "busy@example.com"
    oldest = mock.issue_key(traveler=traveler, channel="cli")
    for _ in range(3):
        mock.issue_key(traveler=traveler, channel="cli")
    muse = mock.issue_key(traveler=traveler, channel="key")
    save_key(home, mock.url, oldest)
    mock.device_traveler = traveler
    mock.device_script = ["approve"]
    try:
        r = run_cli(["login", "--force", "--no-browser"], home=home, api_url=mock.url)
    finally:
        mock.device_traveler = None
    assert r.code == 0, r.stderr
    assert "warning" not in r.stderr
    assert re.search(r"The server retired the previous key [0-9a-f-]{36}\.", r.stdout)
    assert mock.key_record(oldest).revoked is True
    assert mock.key_record(muse).revoked is False, "keys made for other apps are never retired"


def test_shows_the_servers_reason_when_every_connection_belongs_to_another_app(mock, home):
    traveler = "full@example.com"
    for _ in range(5):
        mock.issue_key(traveler=traveler, channel="key")
    mock.device_traveler = traveler
    mock.device_script = ["approve"]
    try:
        r = run_cli(["login", "--json", "--no-browser"], home=home, api_url=mock.url)
    finally:
        mock.device_traveler = None
    assert r.code == 3
    assert r.json_error()["code"] == "access_denied"
    assert r.json_error()["message"] == (
        "This account already has five connections. Disconnect one at /connect/muse, then run `tourclaim login` again. Nothing was saved."
    )


def test_reports_an_existing_valid_key_instead_of_starting_a_new_sign_in(mock, home):
    save_key(home, mock.url, mock.issue_key(channel="cli"))
    before = len(mock.requests_to("POST", "/api/connectors/device/code"))
    r = run_cli(["login", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.json()["already_signed_in"] is True
    assert r.json()["account_email"] == "pat@example.com"
    assert len(mock.requests_to("POST", "/api/connectors/device/code")) == before


def test_force_sends_the_stored_key_with_each_poll_and_the_server_retires_it(mock, home):
    old_key = mock.issue_key(channel="cli")
    save_key(home, mock.url, old_key)
    mock.device_script = ["pending", "approve"]
    before = len(mock.requests)
    r = run_cli(["login", "--force", "--no-browser", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    after = mock.requests[before:]
    polls = [q for q in after if q.path == "/api/connectors/device/token"]
    assert len(polls) == 2 and all(q.headers.get("authorization") == f"Bearer {old_key}" for q in polls)
    assert [q for q in after if q.method == "DELETE"] == [], "the tool never revokes the replaced key itself"
    assert mock.key_record(old_key).revoked is True
    assert store_for(home).get(mock.url).api_key != old_key
    assert r.json()["replaced_key_id"] == mock.key_record(old_key).id
    assert r.json()["previous_key_active"] is False
    assert old_key not in r.stdout + r.stderr
    mock.device_script = ["approve"]
    human = run_cli(["login", "--force", "--no-browser"], home=home, api_url=mock.url)
    assert human.code == 0, human.stderr
    assert re.search(r"The server retired the previous key [0-9a-f-]{36}\.", human.stdout)


def test_force_over_a_key_the_server_does_not_retire_says_it_still_works(mock, home):
    pasted = mock.issue_key(channel="key")
    save_key(home, mock.url, pasted)
    mock.device_script = ["approve"]
    r = run_cli(["login", "--force", "--no-browser"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert mock.key_record(pasted).revoked is False
    assert re.search(r"warning: The previous key [0-9a-f-]{36} still works", r.stderr)


def test_a_dead_stored_key_is_still_sent_and_a_401_for_it_falls_back_to_no_key(home):
    strict = MockServer(device_interval=5, reject_bad_bearer=True)
    strict.start()
    try:
        dead = strict.issue_key(channel="cli")
        strict.keys[dead].revoked = True
        save_key(home, strict.url, dead)
        strict.device_script = ["approve"]
        r = run_cli(["login", "--no-browser"], home=home, api_url=strict.url)
        assert r.code == 0, r.stderr
        polls = strict.requests_to("POST", "/api/connectors/device/token")
        assert [q.headers.get("authorization") for q in polls] == [f"Bearer {dead}", None]
        assert dead not in r.stdout + r.stderr
    finally:
        strict.stop()


def test_with_token_force_revokes_the_previous_stored_key_itself(mock, home):
    old_key = mock.issue_key(channel="cli")
    pasted = mock.issue_key(channel="key")
    save_key(home, mock.url, old_key)
    r = run_cli(["login", "--with-token", "--force"], home=home, api_url=mock.url, stdin=pasted.encode())
    assert r.code == 0, r.stderr
    assert mock.key_record(old_key).revoked is True
    assert "Revoked the previous key" in r.stdout


def test_force_keeps_drafts_started_with_the_old_key_reachable(mock, home):
    mock.device_script = ["approve"]
    assert run_cli(["login", "--no-browser"], home=home, api_url=mock.url).code == 0
    old_key = store_for(home).get(mock.url).api_key
    started = run_cli(["intake", "start", "--json", "--set", "merchant_name=Example Air"], home=home, api_url=mock.url)
    draft_id = started.json()["id"]

    mock.device_script = ["approve"]
    relogin = run_cli(["login", "--force", "--no-browser", "--json"], home=home, api_url=mock.url)
    assert relogin.code == 0, relogin.stderr
    assert mock.key_record(old_key).revoked is True
    assert "no longer be reached" not in relogin.stdout + relogin.stderr

    show = run_cli(["intake", "show", draft_id, "--json"], home=home, api_url=mock.url)
    assert show.code == 0, show.stderr
    listed = run_cli(["intake", "list", "--json"], home=home, api_url=mock.url)
    assert [d["id"] for d in listed.json()] == [draft_id]


def test_refuses_a_key_given_as_an_argument_without_echoing_it(mock, home):
    key = mock.issue_key()
    for argv in (["login", key], ["login", "--with-token", key]):
        r = run_cli(argv, home=home, api_url=mock.url)
        assert r.code == 2
        assert "Never put a key on the command line" in r.stderr
        assert key not in r.stderr and key not in r.stdout
    r = run_cli(["login", f"--token={key}"], home=home, api_url=mock.url)
    assert r.code == 2
    assert key not in r.stderr and "--token" in r.stderr


# ---- login --with-token ----


def test_with_token_reads_a_piped_key_validates_and_saves_it(mock, home):
    key = mock.issue_key(scopes=["claims:read"])
    r = run_cli(["login", "--with-token"], home=home, api_url=mock.url, stdin=f"{key}\n".encode())
    assert r.code == 0, r.stderr
    # A key made at /connect/muse does not say whose account it is.
    assert re.search(r"^Signed in \(key expires \d{4}-\d{2}-\d{2}", r.stdout, re.M)
    assert key not in r.stdout
    saved = json.load(open(creds_file(home), encoding="utf-8"))[mock.url]
    assert saved["api_key"] == key
    assert saved["scopes"] == ["claims:read"]
    assert saved["grant_id"] == mock.key_record(key).id


def test_with_token_reads_from_a_hidden_prompt_on_a_terminal(mock, home):
    key = mock.issue_key()
    r = run_cli(["login", "--with-token", "--json"], home=home, api_url=mock.url, stdin_is_tty=True, answers=[key])
    assert r.code == 0, r.stderr
    assert len(r.prompts) == 1
    assert "account_email" not in r.json(), "no account_email for a pasted key"
    assert key not in r.stdout


def test_with_token_exits_3_and_saves_nothing_when_rejected(mock, home):
    key = mock.issue_key()
    mock.keys[key].revoked = True
    r = run_cli(["login", "--with-token"], home=home, api_url=mock.url, stdin=key.encode())
    assert r.code == 3
    assert key not in r.stderr
    assert not os.path.exists(creds_file(home))


def test_with_token_rejects_text_that_is_not_a_key(mock, home):
    r = run_cli(["login", "--with-token"], home=home, api_url=mock.url, stdin=b"hello world")
    assert r.code == 2
    assert "does not look like a TourClaim key" in r.stderr


# ---- credentials ----


def test_env_key_takes_precedence_over_the_stored_key(mock, home):
    stored = mock.issue_key(traveler="stored@example.com")
    from_env = mock.issue_key(traveler="env@example.com", channel="cli")
    save_key(home, mock.url, stored)
    r = run_cli(["status", "--json"], home=home, api_url=mock.url, env={"TOURCLAIM_API_KEY": from_env})
    assert r.code == 0, r.stderr
    assert r.json()["account_email"] == "env@example.com"
    assert r.json()["key_source"] == "env"
    assert mock.requests_to("GET", "/api/connectors/v1/key")[-1].headers["authorization"] == f"Bearer {from_env}"
    assert from_env not in r.stdout + r.stderr and stored not in r.stdout + r.stderr


def test_stores_keys_per_api_url(home):
    store = store_for(home)
    from tourclaim.credentials import StoredCredential

    store.set("https://a.example", StoredCredential("tc_muse_a"))
    store.set("https://b.example", StoredCredential("tc_muse_b"))
    assert store.get("https://a.example").api_key == "tc_muse_a"
    assert store.remove("https://a.example") is True
    assert store.remove("https://a.example") is False
    assert store.get("https://a.example") is None
    assert store.get("https://b.example").api_key == "tc_muse_b"
    store.remove("https://b.example")
    assert not os.path.exists(store.path), "an empty store deletes the file"


def test_credentials_file_format_matches_the_node_edition(home):
    """JSON.stringify(data, null, 2) + "\\n", the Node edition's exact bytes."""
    from tourclaim.credentials import StoredCredential

    store = store_for(home)
    store.set(
        "https://app.getcopernican.com",
        StoredCredential("tc_muse_abc", "2026-11-03T18:00:00Z", "g-1", ["intakes:write", "claims:read"]),
    )
    store.set("http://127.0.0.1:4010", StoredCredential("tc_muse_def"))
    expected = (
        "{\n"
        '  "https://app.getcopernican.com": {\n'
        '    "api_key": "tc_muse_abc",\n'
        '    "expires_at": "2026-11-03T18:00:00Z",\n'
        '    "grant_id": "g-1",\n'
        '    "scopes": [\n'
        '      "intakes:write",\n'
        '      "claims:read"\n'
        "    ]\n"
        "  },\n"
        '  "http://127.0.0.1:4010": {\n'
        '    "api_key": "tc_muse_def",\n'
        '    "expires_at": null,\n'
        '    "grant_id": null,\n'
        '    "scopes": []\n'
        "  }\n"
        "}\n"
    )
    with open(store.path, "rb") as handle:
        assert handle.read() == expected.encode("utf-8")


def test_reads_a_credentials_file_written_by_the_node_edition(mock, home):
    key = mock.issue_key(traveler="node@example.com", channel="cli")
    path = creds_file(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    node_written = json.dumps(
        {mock.url: {"api_key": key, "expires_at": "2026-11-03T18:00:00", "grant_id": "g", "scopes": ["claims:read"]}}, indent=2
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(node_written + "\n")
    if not IS_WINDOWS:
        os.chmod(path, 0o600)
    r = run_cli(["status", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.json()["account_email"] == "node@example.com"
    assert r.json()["key_source"] == "stored"


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX file modes")
def test_tightens_an_existing_file_and_directory_to_0600_0700(home):
    from tourclaim.credentials import StoredCredential

    path = creds_file(home)
    directory = os.path.dirname(path)
    os.makedirs(directory)
    os.chmod(directory, 0o755)
    with open(path, "w") as handle:
        handle.write("{}")
    os.chmod(path, 0o644)
    store_for(home).set("https://x.example", StoredCredential("tc_muse_x"))
    assert mode_bits(path) == 0o600
    assert mode_bits(directory) == 0o700


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX file modes")
def test_warns_when_the_credentials_file_is_readable_by_others(mock, home):
    save_key(home, mock.url, mock.issue_key())
    os.chmod(creds_file(home), 0o644)
    r = run_cli(["status"], home=home, api_url=mock.url)
    assert "can be read by other users" in r.stderr


def test_rejects_a_corrupt_credentials_file(mock, home):
    path = creds_file(home)
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as handle:
        handle.write("not json")
    r = run_cli(["claims", "list", "--json"], home=home, api_url=mock.url)
    assert r.code == 1
    assert r.json_error()["code"] == "bad_credentials_file"


def test_warns_when_the_stored_key_expires_within_3_days(mock, home):
    key = mock.issue_key()
    soon = (datetime.now(timezone.utc) + timedelta(days=2, hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    save_key(home, mock.url, key, soon)
    r = run_cli(["claims", "list"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert "warning: Your TourClaim key expires in 2 days" in r.stderr
    later = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_key(home, mock.url, key, later)
    quiet = run_cli(["claims", "list"], home=home, api_url=mock.url)
    assert "expires" not in quiet.stderr


def test_uses_xdg_config_home_and_appdata_on_windows():
    assert credentials_path({"XDG_CONFIG_HOME": "/x/cfg"}, "linux", "/home/p") == "/x/cfg/tourclaim/credentials.json"
    assert credentials_path({}, "darwin", "/Users/p") == "/Users/p/.config/tourclaim/credentials.json"
    assert credentials_path({"XDG_CONFIG_HOME": "relative"}, "linux", "/home/p") == "/home/p/.config/tourclaim/credentials.json"
    assert (
        credentials_path({"APPDATA": "C:\\Users\\p\\AppData\\Roaming"}, "win32", "C:\\Users\\p")
        == "C:\\Users\\p\\AppData\\Roaming\\tourclaim\\credentials.json"
    )
    assert credentials_path({}, "win32", "C:\\Users\\p") == "C:\\Users\\p\\AppData\\Roaming\\tourclaim\\credentials.json"


# ---- logout and status ----


def test_logout_revokes_the_key_and_removes_it_locally(mock, home):
    key = mock.issue_key()
    save_key(home, mock.url, key)
    r = run_cli(["logout", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.json() == {"api_url": mock.url, "revoked": True, "already_invalid": False, "credential_removed": True, "key_source": "stored"}
    assert mock.key_record(key).revoked is True
    assert not os.path.exists(creds_file(home))


def test_logout_removes_the_stored_key_even_when_the_server_says_401(mock, home):
    key = mock.issue_key()
    mock.keys[key].revoked = True
    save_key(home, mock.url, key)
    r = run_cli(["logout"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert "already expired or revoked" in r.stdout
    assert not os.path.exists(creds_file(home))


def test_logout_keeps_the_key_when_the_server_cannot_be_reached(mock, home):
    save_key(home, mock.url, mock.issue_key())
    mock.enabled = False
    r = run_cli(["logout"], home=home, api_url=mock.url)
    assert r.code == 7
    assert "still stored" in r.stderr
    assert os.path.exists(creds_file(home))


def test_logout_when_not_signed_in(mock, home):
    r = run_cli(["logout", "--json"], home=home, api_url=mock.url)
    assert r.code == 0
    assert r.json()["revoked"] is False and r.json()["key_source"] is None


def test_status_shows_review_mode_the_account_and_key_expiry(mock, home):
    save_key(home, mock.url, mock.issue_key(channel="cli"))
    r = run_cli(["status"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    assert r.stdout.split("\n")[0] == "REVIEW MODE: CLAIMS ARE SYNTHETIC AND NOTHING IS FILED"
    assert re.search(r"Signed in:\s+yes, as pat@example\.com", r.stdout)
    assert re.search(r"Key:\s+[0-9a-f-]{36}, expires \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", r.stdout)
    assert re.search(rf"Manage keys:\s+{re.escape(mock.url)}/connect/muse", r.stdout)


def test_status_without_an_account_email_for_a_pasted_key(mock, home):
    save_key(home, mock.url, mock.issue_key(channel="key"))
    human = run_cli(["status"], home=home, api_url=mock.url)
    assert re.search(r"^Signed in:\s+yes$", human.stdout, re.M)
    js = run_cli(["status", "--json"], home=home, api_url=mock.url).json()
    assert js["signed_in"] is True and "account_email" not in js


def test_whoami_is_an_alias_and_json_carries_account_and_mode(mock, home):
    save_key(home, mock.url, mock.issue_key(channel="cli"))
    r = run_cli(["whoami", "--json"], home=home, api_url=mock.url)
    assert r.code == 0, r.stderr
    s = r.json()
    assert s["mode"] == "review" and s["enabled"] is True and s["signed_in"] is True
    assert s["account_email"] == "pat@example.com"
    assert s["connector"]["openapi_url"] == f"{mock.url}/api/connectors/v1/openapi.json"
    assert s["connector"]["cli_login_url"] == f"{mock.url}/connect/cli"
    assert s["credentials_path"] == creds_file(home)


def test_status_accepts_relative_discovery_urls_from_older_servers(home):
    old = MockServer(relative_discovery=True)
    old.start()
    try:
        r = run_cli(["status", "--json"], home=home, api_url=old.url)
        connector = r.json()["connector"]
        assert connector["connection_url"] == f"{old.url}/connect/muse"
        assert connector["openapi_url"] == f"{old.url}/api/connectors/v1/openapi.json"
        assert connector["cli_login_url"] == f"{old.url}/connect/cli"
    finally:
        old.stop()


def test_status_exits_3_when_not_signed_in_and_7_when_disabled(mock, home):
    r = run_cli(["status", "--json"], home=home, api_url=mock.url)
    assert r.code == 3
    assert r.json()["signed_in"] is False
    mock.enabled = False
    r = run_cli(["status", "--json"], home=home, api_url=mock.url)
    assert r.code == 7
    assert r.json()["enabled"] is False


def test_status_reports_a_rejected_key(mock, home):
    key = mock.issue_key()
    mock.keys[key].revoked = True
    save_key(home, mock.url, key)
    r = run_cli(["status"], home=home, api_url=mock.url)
    assert r.code == 3
    assert "the key was rejected" in r.stdout
