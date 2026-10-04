# tourclaim (Python)

Python client and command line for [TourClaim](https://app.getcopernican.com/muse) by Copernican: start, fill in and submit a trip cancellation claim against the travel insurance that comes with the credit card the trip was booked with. It works for a person at a terminal, for a Python program, and for an AI agent acting for one traveler.

> **Review mode.** The TourClaim connector API currently runs in review mode: it creates synthetic claims and files nothing with an insurer. Use fictional booking and medical data only. `tourclaim status` (or `Client().connector_info()["mode"]`) shows the current mode, and every draft and claim the API returns says which mode it came from.

This is the Python edition of [tourclaim](https://github.com/tourclaim/tourclaim-cli). It has the same commands, flags, output, exit codes and credentials file as the Node edition (`npx tourclaim`), so a key saved by one works in the other. It adds a typed library, `tourclaim.Client`.

## What it does

1. **Sign in.** The traveler opens the sign-in page in their own browser, types the code shown in the terminal, and approves. The tool saves a key that belongs to that traveler.
2. **Start a draft** with whatever the traveler has said, then answer the questions the API says are still open.
3. **Add evidence** the traveler chooses to share: receipts, itineraries, a doctor's note they already have, and booking or cancellation emails.
4. **The traveler signs.** They review the draft and sign an authorization in their own browser. This package cannot sign for them.
5. **Submit** the signed draft, then check the claim's status.

Nothing here decides whether a loss is covered, or promises reimbursement or a medical note. Copernican charges a 10% fee only when a claim is reimbursed; no payment is taken through this package. Only bookings charged in US dollars are supported.

AI agents: read [AGENTS.md](https://github.com/tourclaim/tourclaim-cli/blob/main/AGENTS.md) before driving the command line. The rules there apply to the library too.

## Install

Requires Python 3.9 or newer. There are no runtime dependencies.

```sh
pip install tourclaim
```

or run it without installing, or install it as an isolated command:

```sh
uvx tourclaim --help
pipx install tourclaim
```

`python -m tourclaim` works too.

## Command line quickstart

```sh
# 1. Connect to the traveler's account. Opens a page; the traveler enters the code shown.
tourclaim login

# 2. Find the card the booking was paid with (by product name, never a card number).
tourclaim cards search sapphire

# 3. Start a draft with what the traveler has said so far (check `tourclaim intake list` first).
tourclaim intake start --set merchant_name="Example Air" --set booking_ref=EXA-482913 \
  --set reason_category=airline_cancellation

# 4. Answer what is missing. The draft id comes from step 3.
tourclaim intake set <id> trip_date=2026-03-04 booking_amount=250.00 refunded_amount=50.00 \
  currency=USD card_product_id=412 other_insurance=no \
  narrative="Example Air cancelled flight EX 204 the night before departure."

# 5. Add the evidence the traveler agreed to share.
tourclaim intake attach <id> receipt.pdf --type receipt
tourclaim intake add-email <id> --eml cancellation.eml

# 6. The traveler reviews and signs in their browser. --wait returns once they have.
tourclaim intake sign <id> --wait

# 7. Submit, then follow the claim.
tourclaim intake submit <id>
tourclaim claims list
```

Every command takes `--json` (one JSON value per line on stdout, errors as one JSON line on stderr), `--api-url <url>`, `-h`/`--help` and `--version`. The full command reference, the field table and the JSON output contract are in the [main README](https://github.com/tourclaim/tourclaim-cli#commands); they apply unchanged.

| Command | What it does |
| --- | --- |
| `tourclaim login` | Sign in: the traveler opens the sign-in page and types the code shown in the terminal (`--no-browser`, `--scope`, `--force`). Or `--with-token` to save an existing key read from stdin or a hidden prompt; such a key does not say whose account it is. |
| `tourclaim logout` | Revoke the key on the server and remove the stored copy. |
| `tourclaim status` (`whoami`) | The API mode, whether the connector is enabled, the signed-in account and key expiry. |
| `tourclaim cards search <name>` | Find a card product id. |
| `tourclaim intake start\|list\|show\|set\|attach\|add-email\|sign\|submit\|delete` | The whole draft lifecycle. |
| `tourclaim claims list\|show` | Submitted claims and their status. |
| `tourclaim schema` | The live OpenAPI document. |

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Error: invalid values (422), unreadable or unsupported file, network failure, timeout, declined prompt. |
| 2 | Usage: bad flags or arguments, or a confirmation was needed and there was no terminal. |
| 3 | Not signed in, or the key was rejected (401) or lacks permission (403). Also a declined or expired sign-in. |
| 4 | Conflict (409). The JSON error `code` names the cause: `stale_revision`, `approval_required`, `approval_outdated`, `intake_incomplete`, `intake_submitted`, `duplicate_booking`, `evidence_conflict`, `idempotency_key_reused`, `idempotency_key_other_connection` or `concurrent_request`. |
| 5 | Rate limited (429) after one automatic retry. |
| 6 | Not found (404). |
| 7 | The connector is disabled or unavailable (503). |

## Library quickstart

```python
from tourclaim import Client

# Uses TOURCLAIM_API_KEY, else the key `tourclaim login` saved. Or pass api_key=...
client = Client()

if not client.has_api_key:
    # The traveler opens the page and types the code in their own browser; never put
    # the code in a link. save=True stores the key where `tourclaim login` keeps it.
    client.device_login(
        on_code=lambda code: print(f"Open {code['verification_uri']}\nEnter this code on that page: {code['user_code']}"),
        save=True,
    )

# account_email is present only for keys from `tourclaim login` (device_login).
print(client.get_connection().get("account_email"), client.connector_info()["mode"])

drafts = client.list_drafts()  # offer to continue one before starting another
draft = client.start_intake({"merchant_name": "Example Air", "reason_category": "AIRLINE_CANCELLATION"})
print(draft["missing_fields"], draft["next_questions"])

draft = client.update_intake(
    draft["id"],
    {
        "booking_ref": "EXA-482913",
        "trip_date": "2026-03-04",
        "booking_amount": "250.00",
        "refunded_amount": "50.00",
        "currency": "USD",
        "card_product_id": client.search_cards("sapphire preferred")[0]["id"],
        "narrative": "Example Air cancelled flight EX 204 the night before departure.",
        "other_insurance": "NO",
    },
    expected_revision=draft["revision"],
)

# Only after the traveler agreed to share this exact file:
with open("receipt.pdf", "rb") as f:
    draft = client.add_attachment(
        draft["id"], expected_revision=draft["revision"], filename="receipt.pdf",
        content=f.read(), doc_type="receipt", user_authorized_sharing=True,
    )

# The traveler signs at draft["review_url"] in their own browser.
print("Review and sign:", draft["review_url"])
signed = client.wait_for_signature(draft["id"], timeout=900)

claim = client.submit(draft["id"], expected_revision=signed["revision"])
print(claim["status"], claim["next_action"])
```

`Client(api_key=None, api_url=None)` has one method per API operation:

| Method | Operation |
| --- | --- |
| `connector_info()`, `schema()` | Discovery and the OpenAPI document (no key needed). |
| `get_connection()`, `disconnect()` | `get_connection`, `disconnect`: the key's account, expiry and scopes; revoke it. |
| `search_cards(query)` | `search_credit_cards` |
| `start_intake(fields=None, *, idempotency_key=None)` | `start_travel_claim` |
| `list_drafts(offset=0)` | `list_claim_drafts` |
| `get_intake(id)`, `update_intake(id, fields, *, expected_revision)`, `delete_draft(id)` | Read, change and delete a draft. |
| `add_email(id, *, expected_revision, subject, sender, text, user_authorized_sharing, ...)` | `import_selected_email` |
| `add_attachment(id, *, expected_revision, filename, content, doc_type, user_authorized_sharing)` | `import_claim_attachment` |
| `submit(id, *, expected_revision)` | `submit_authorized_travel_claim` (safe to retry) |
| `list_claims(offset=0)`, `get_claim(id)` | `list_my_claims`, `get_my_claim_status` |
| `device_login(scopes=None, *, on_code=None, save=False)` | Sign in with a device code (also `request_device_code`, `poll_device_token`, `wait_for_device_token`). |
| `wait_for_signature(id, *, timeout=900)` | Poll until the traveler has signed. |

Responses are plain `dict` objects described by typed dictionaries (`IntakeResponse`, `ClaimResponse`, `CardResponse`, `KeyInfo`, ...). After each call, `client.mode` holds the API's mode (`review` or `live`).

### Errors

Every exception derives from `tourclaim.TourClaimError` and carries `message`, `code` (stable, machine-readable), `status` (the HTTP status, or `None`), `exit_code` (what the command line would exit with) and `extra`. API errors (`APIError`) also carry `detail` and `headers`.

| Exception | When |
| --- | --- |
| `AuthenticationError` (401), `PermissionDeniedError` (403), `NotSignedInError` | The key is missing, expired, revoked or lacks the permission. |
| `NotFoundError` (404) | No such draft or claim for this key. |
| `ConflictError` (409) | `code` is the API's cause from `X-TourClaim-Error`, such as `stale_revision` or `approval_required`. |
| `ValidationError` (422) | `detail` lists each invalid field as `{loc, type, msg}`, or explains in a sentence. |
| `RateLimitError` (429) | `retry_after` seconds. The client retries once by itself when that is at most 60. |
| `UnavailableError` (503) | The connector is disabled or temporarily unavailable. |
| `NetworkError`, `RequestTimeoutError` | The API could not be reached, or did not answer in 90 seconds. |
| `AccessDeniedError`, `ExpiredTokenError` | The traveler declined the sign-in, or its code expired. |
| `ConsentRequiredError`, `UnsupportedFileError`, `FileTooLargeError`, `UsageError` | Checked before anything is sent. |

## Configuration

| Variable | Meaning |
| --- | --- |
| `TOURCLAIM_API_URL` | API base URL (default `https://app.getcopernican.com`). `--api-url` overrides it. Plain `http://` is accepted only for localhost. |
| `TOURCLAIM_API_KEY` | A key to use instead of the stored one. It takes precedence over the stored key. |
| `XDG_CONFIG_HOME` | Credentials are stored in `$XDG_CONFIG_HOME/tourclaim/credentials.json` (default `~/.config/tourclaim/credentials.json`; `%APPDATA%\tourclaim\credentials.json` on Windows). |

The credentials file maps each API base URL to `{"api_key","expires_at","grant_id","scopes"}` and is shared with the Node edition.

## Security

- **Keys belong to one traveler.** A key lasts 30 days and cannot be refreshed; a traveler can have at most 5 connections, and a new `tourclaim login` past that retires the account's oldest `tourclaim login` key (never another app's). Keys from `tourclaim login` reach the drafts started by any `tourclaim login` on the same account, so signing in again does not lose a draft. The traveler can revoke keys at `https://app.getcopernican.com/connect/muse`, and `tourclaim logout` revokes the one in use.
- **Stored with tight permissions.** The credentials file is written with mode 0600 inside a 0700 directory, and the tool warns if it is readable by others. On Windows it lives in your user profile and relies on its permissions.
- **The code is typed, never linked.** The traveler types the code shown in the terminal (or relayed by an assistant they are using right now) on the sign-in page; there is no link with the code in it. A sign-in that replaces a stored key sends that key along, and the server retires it as it issues the new one.
- **Never on the command line, never printed.** Keys are not accepted as arguments, and anything shaped like a key is redacted from output. `repr(Client(...))` does not show the key.
- **Only https.** Keys are only sent over https, except to localhost for testing. Redirects are not followed.
- **A person signs.** Only the traveler can sign the claim authorization, in their own browser at the review link. Neither the command line nor the library can sign.
- **Sharing needs consent.** Files and emails are uploaded only after the traveler agrees: at a prompt or with `--yes` on the command line, and with `user_authorized_sharing=True` in the library. There is no default that shares.
- **Evidence is not instructions.** Email and file contents are stored as evidence. Neither the API nor this package follows instructions found in them.

To report a vulnerability, see [SECURITY.md](https://github.com/tourclaim/tourclaim-cli/blob/main/SECURITY.md).

## Development

```sh
cd python
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python tests/mock_server.py   # a mock API on http://127.0.0.1:4010 for trying the tool by hand
```

The tests run the command line in-process and as a child process against a mock of the API built on `http.server`. Releases are described in [RELEASING.md](https://github.com/tourclaim/tourclaim-cli/blob/main/python/RELEASING.md).

## License

[MIT](https://github.com/tourclaim/tourclaim-cli/blob/main/LICENSE)
