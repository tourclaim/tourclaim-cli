# tourclaim

Command-line client for [TourClaim](https://app.getcopernican.com/muse) by Copernican: start, fill in and submit a trip cancellation claim against the travel insurance that comes with the credit card the trip was booked with. It works for a person at a terminal and for an AI agent acting for one traveler.

> **Review mode.** The TourClaim connector API currently runs in review mode: it creates synthetic claims and files nothing with an insurer. Use fictional booking and medical data only. `tourclaim status` shows the current mode, and every intake and claim the API returns says which mode it came from.

## What it does

TourClaim files trip cancellation and interruption claims against the travel benefits of a traveler's credit card. This tool drives TourClaim's connector API on behalf of one traveler:

1. **Sign in.** The traveler opens the sign-in page in their own browser, types the code shown in the terminal, and approves. The tool saves a key that belongs to that traveler.
2. **Start a draft** with whatever the traveler has said, then answer the questions the API says are still open.
3. **Add evidence** the traveler chooses to share: receipts, itineraries, a doctor's note they already have, and booking or cancellation emails.
4. **The traveler signs.** They review the draft and sign an authorization in their own browser. This tool cannot sign for them.
5. **Submit** the signed draft, then check the claim's status.

Nothing in this tool or the API decides whether a loss is covered, or promises reimbursement or a medical note. Copernican charges a 10% fee only when a claim is reimbursed; no payment is taken through this tool. Only bookings charged in US dollars are supported.

For AI agents, read [AGENTS.md](AGENTS.md) before driving this tool.

## Install

Requires Node.js 18.3 or newer. There are no runtime dependencies.

```sh
npx tourclaim --help
```

or install it:

```sh
npm install -g tourclaim
tourclaim --help
```

## Quickstart

```sh
# 1. Connect to the traveler's account. Opens a page; the traveler enters the code shown.
tourclaim login

# 2. Find the card the booking was paid with (by product name, never a card number).
tourclaim cards search sapphire

# 3. Start a draft with what the traveler has said so far.
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

## Commands

Every command accepts these global options:

| Option | Meaning |
| --- | --- |
| `--json` | Machine-readable output. See [Machine-readable output](#machine-readable-output). |
| `--api-url <url>` | API base URL. Default `https://app.getcopernican.com`, or `TOURCLAIM_API_URL`. |
| `-h`, `--help` | Help for the command. `tourclaim help <command>` works too. |
| `--version` | Print the version. |

Commands never prompt when stdin is not a terminal; they fail with a message saying which flag to pass instead.

### `tourclaim login`

```
tourclaim login [--no-browser] [--scope <scope>]... [--force]
tourclaim login --with-token [--force] < key.txt
```

Signs in with a device code. The tool prints the sign-in page (`https://app.getcopernican.com/connect/cli`) and, on a line of its own, `Enter this code on that page: WDJB-MJHT`. It opens the page in the browser when run in a terminal (not with `--no-browser`). The traveler types the code shown in the terminal and approves; the code is never put in a link, so a link someone else sends cannot sign anyone in. The tool polls at the interval the API asks for, slows down when told to, and gives up when the code expires. It then saves the key and prints the account and expiry: `Signed in as pat@example.com (key expires 2026-11-03 18:00 UTC, in 30 days).`

- `--scope` asks for fewer permissions: any of `intakes:write`, `evidence:write`, `claims:submit`, `claims:read` (repeat the flag or separate with commas). The default is all four.
- If a valid key is already stored for the API URL, `login` reports it and exits 0 without starting a new sign-in. `--force` gets a new key. Whenever a sign-in replaces a stored key, the tool sends that key along with the sign-in, and the server retires it as it issues the new one (if it is a `tourclaim login` key for the same account; otherwise the tool warns that the old key still works). Drafts are not lost: see [Which drafts a key can reach](#which-drafts-a-key-can-reach).
- The tool waits until the code expires and then checks once more, so an approval made in the last seconds still signs in. While it waits, a brief server error (HTTP 502, 503 or 504) or a dropped connection does not end the sign-in: it tries again, honoring `Retry-After`, and gives up only after 3 failures in a row.
- A traveler can hold at most 5 connections. When a new `tourclaim login` would go over, the server retires the account's oldest `tourclaim login` key; it never touches keys made for other apps. If all 5 belong to other apps, the sign-in fails (exit 3) with the server's reason: disconnect one at `https://app.getcopernican.com/connect/muse` first.
- `--with-token` saves a key the traveler already created at `https://app.getcopernican.com/connect/muse`. It reads the key from stdin when piped, or from a hidden prompt on a terminal, and checks it with the API before saving. Such a key does not say whose account it is, so the tool prints `Signed in (key expires ...)` without an email. A key is never accepted as a command-line argument.
- With `--json`, the first line is `{"event":"device_code","user_code":...,"verification_uri":...,"expires_in":...,"interval":...}` so an agent can show the page and the code to its human; the last line is `{"event":"signed_in",...}`, with `account_email` when the key says whose account it is.

### `tourclaim logout`

Revokes the key in use on the server, then deletes the stored copy. The stored copy is deleted even when the server says the key was already invalid. If the server cannot be reached, nothing is deleted, so you can try again.

### `tourclaim status` (alias `whoami`)

Shows the API URL, whether the connector is enabled, the mode, the signed-in account, key expiry and permissions. The first line in review mode is `REVIEW MODE: CLAIMS ARE SYNTHETIC AND NOTHING IS FILED`. Exits 0 when signed in, 3 when not signed in or the key is rejected, 7 when the connector is disabled.

### `tourclaim cards search <query...>`

Searches the card catalog by product name (at most 100 characters, at most 30 matches). Use a result's `id` as `card_product_id`. Finding a card does not mean it covers the loss. The command refuses input that looks like a card number.

### `tourclaim intake start`

```
tourclaim intake start [--set <field>=<value>]... [--fields-file <file>] [--idempotency-key <key>]
```

Starts a draft. Every field is optional at this point; the output lists what is missing and up to three questions to ask next. Run `tourclaim intake list` first: the traveler may already have a draft for the same trip. The command sends a random `Idempotency-Key` unless you give one. If it fails or times out, run it again with the same `--idempotency-key` and the same fields to get the same draft rather than a second one; the error message includes the key it used.

### `tourclaim intake list [--offset <n>]`

Lists the traveler's drafts that were never submitted, most recently changed first, 30 at a time, with each one's state, merchant, booking reference and how many answers are missing. Use it to pick up a draft whose id was lost. Submitted drafts are claims; see `tourclaim claims list`.

### `tourclaim intake show <id>`

Shows the saved answers, evidence, state, revision and what is still missing.

### `tourclaim intake set <id> ...`

```
tourclaim intake set <id> [<field>=<value>]... [<field>:=<json>]... [--clear <field>]... [--fields-file <file>] [--revision <n>]
```

Saves only the fields given; everything else is unchanged. `--clear <field>` clears a field (sends `null`).

- `field=value` is typed by the schema: amounts such as `250.00`, dates such as `2026-03-04`, `true`/`false` (or `yes`/`no`), and enum values in any case (`weather` becomes `WEATHER`).
- `field:=<json>` sends a JSON value as it is, for example `card_product_id:=412` or `medical:='{"provider_seen":true}'`.
- `--fields-file` reads a JSON object of fields (`-` reads stdin). Pairs given on the command line override it.
- The command reads the draft's current revision and sends it as `expected_revision` (or uses `--revision`). If the draft changed in between, nothing is saved: the command exits 4 and says what the draft looks like now. It never retries on its own.
- Any change after the traveler signed cancels the signature, and the command says so. They must sign again.

| Field | Type | Notes |
| --- | --- | --- |
| `merchant_name` | text | Who the traveler booked with, as on the confirmation. |
| `booking_ref` | text | Booking reference exactly as printed. One claim per booking. |
| `trip_date` | date | First day of the trip, `YYYY-MM-DD`. |
| `booking_amount` | amount | Total paid, USD, such as `250.00`. |
| `refunded_amount` | amount | Already refunded; `0.00` if nothing. |
| `currency` | `USD` | Only USD bookings are supported. |
| `card_product_id` | integer | From `tourclaim cards search`. Never a card number. |
| `reason_category` | enum | `ILLNESS`, `INJURY`, `DEATH_IN_FAMILY`, `MILITARY_DEPLOYMENT`, `WEATHER`, `AIRLINE_CANCELLATION`, `OTHER`. |
| `narrative` | text | What happened, in the traveler's own words (10 to 10,000 characters). |
| `cancellation_policy` | text | The merchant's terms, if the traveler has them. Optional. |
| `other_insurance` | enum | `YES`, `NO`, `UNSURE`. |
| `other_insurance_details` | text | Required when `other_insurance` is `YES`. |
| `medical.symptom_onset_date` | date | Asked when the reason is `ILLNESS` or `INJURY`. |
| `medical.provider_seen` | true/false | |
| `medical.provider_details` | text | Optional. |
| `medical.prior_condition` | true/false | |
| `medical.prior_condition_details` | text | Optional. |
| `medical.stability_60day` | true/false | Treated for this condition in the 60 days before booking. |
| `medical.stability_60day_details` | text | Optional. |

### `tourclaim intake attach <id> <file> --type <type>`

```
tourclaim intake attach <id> <file> --type receipt|itinerary|medical_note|other [--yes] [--revision <n>]
```

Uploads one PDF, JPEG or PNG of at most 5 MiB. The type is detected from the file's first bytes, not its name. Sharing needs the traveler's agreement: on a terminal the command asks; otherwise it needs `--yes`, and without it the command refuses and uploads nothing. There is no setting that makes sharing the default. A `medical_note` here is a note the traveler already has; it is not a note issued by a Copernican provider.

### `tourclaim intake add-email <id>`

```
tourclaim intake add-email <id> (--eml <file> | --text-file <file> --subject <text> --from <address>)
    [--sent-at <iso>] [--provider user|gmail|outlook] [--message-id <id>] [--yes] [--revision <n>]
```

Saves one email as evidence. With `--eml`, the Subject, From, Date and Message-ID headers and the first `text/plain` part are read from the saved message (flags override them). With `--text-file`, give `--subject` and `--from`. The text is sent unmodified, at most 30,000 characters. Without `--message-id`, a stable id is derived from the message, so adding the same message twice changes nothing. `--provider` defaults to `user` (pasted or saved by the traveler). Consent works as for `attach`.

### `tourclaim intake sign <id>`

```
tourclaim intake sign <id> [--wait] [--timeout <seconds>] [--no-browser]
```

Only the traveler can sign, in their own browser. When the draft is complete, this prints the review link (`review_url`) and opens it when run in a terminal. `--wait` checks every 5 seconds until the traveler has signed or `--timeout` (default 900 seconds) passes; like `login`, it rides out up to 2 brief server errors or dropped connections in a row. If the draft is already signed or submitted, it says so. If answers are missing, it lists them. This tool never signs and does not automate the review page. If the review page says "Your command-line sign-in has ended", run `tourclaim login`, then reload the page; the draft is kept.

### `tourclaim intake submit <id>`

Submits a signed draft as a claim and prints the claim. Safe to retry: a repeated call returns the same claim. Exits 4 when the draft changed since `--revision` (`stale_revision`), the traveler has not signed the current revision (`approval_required`), the draft is incomplete (`intake_incomplete`), the signature is older than 7 days or the authorization changed (`approval_outdated`), or the booking already has a claim (`duplicate_booking`). Submitting does not file anything with an insurer, charge a fee or promise reimbursement; Copernican reviews the claim next.

### `tourclaim intake delete <id>`

Permanently deletes a draft that was never submitted, with everything saved against it. Asks for confirmation, or takes `--yes`. A submitted draft cannot be deleted here; see `https://app.getcopernican.com/muse/data-deletion`.

### `tourclaim claims list [--offset <n>]`

Lists submitted claims, newest first, 30 at a time. Drafts that were never submitted are not listed.

### `tourclaim claims show <id>`

Shows a claim's status, the next action in words to pass on to the traveler, and when it last changed. A status is not a coverage decision unless it says so.

### `tourclaim schema`

Prints the live OpenAPI document from `/api/connectors/v1/openapi.json`. No sign-in needed. A copy of the schema this version was built against is in [openapi/connectors-v1.json](openapi/connectors-v1.json).

## Draft states

| State | Meaning | Next |
| --- | --- | --- |
| `collecting` | Answers are missing. | `tourclaim intake set` |
| `needs_approval` | Complete; the traveler must review and sign. | `tourclaim intake sign` |
| `ready_to_submit` | Signed. | `tourclaim intake submit` |
| `submitted` | A claim exists; the draft is read-only. | `tourclaim claims show` |

## Machine-readable output

With `--json`:

- **stdout** carries one JSON value per line. For `cards search`, `intake start|show|set|attach|add-email|submit` and `claims list|show`, it is the API's own response object, unchanged. For `intake sign` without `--wait`, it is the draft.
- **Commands that wait** print an event line first and the result last: `login` prints `{"event":"device_code",...}` then `{"event":"signed_in",...}`; `intake sign --wait` prints `{"event":"waiting_for_signature","intake_id","review_url","timeout_seconds"}` then the draft. The last line is always the result.
- **`status`** prints `{"api_url","mode","enabled","connector":{...},"signed_in","account_email","key":{"id","expires_at","scopes"},"key_source","key_problem","credentials_path"}`; `account_email` is present only for keys from `tourclaim login`. **`logout`** prints `{"api_url","revoked","already_invalid","credential_removed","key_source"}`. **`intake delete`** prints `{"id","deleted":true}`.
- **Errors** go to stderr as one line, `{"error":{"code","message","exit_code",...}}`, with `status`, `detail` (the API's validation list), `retry_after`, `idempotency_key`, `review_url`, `current_revision`, `state` or `missing_fields` when they apply. Codes include `usage_error`, `not_signed_in`, `unauthorized`, `forbidden`, `not_found`, `invalid`, `too_large`, `rate_limited`, `unavailable`, `access_denied`, `expired_token`, `timeout`, `network_error`, `unsupported_file`, `cancelled`, and for exit code 4 one of the [conflict causes](#conflict-causes).
- **Progress and warnings** go to stderr as plain text in both modes.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Error: invalid values (422), unreadable or unsupported file, network failure, timeout, declined prompt. |
| 2 | Usage: bad flags or arguments, or a confirmation was needed and there was no terminal (pass `--yes` only with the traveler's agreement). |
| 3 | Not signed in, or the key was rejected (401) or lacks permission (403). Also a declined or expired sign-in. Run `tourclaim login`. |
| 4 | Conflict (409). The JSON error `code` says which; see [Conflict causes](#conflict-causes). |
| 5 | Rate limited (429) after one automatic retry. The tool waits `Retry-After` (up to 60 seconds) and retries once by itself. |
| 6 | Not found (404). |
| 7 | The connector is disabled or unavailable (503). |

## Conflict causes

Exit code 4 means the API refused a change because of the draft's state. The API names the cause in its `X-TourClaim-Error` header, and the tool uses it as the JSON error `code`. The tool also uses these codes when it refuses before calling the API (for example, submitting a draft that is not signed). It never retries a conflict on its own.

| Code | Meaning | What to do |
| --- | --- | --- |
| `idempotency_key_reused` | This `--idempotency-key` was used with different fields. | Leave the key out (or use a new one) to start a different draft. |
| `idempotency_key_other_connection` | The key was used by another connection. | Use a new key. |
| `concurrent_request` | The same request ran twice at once. | Run it again with the same `--idempotency-key`. |
| `stale_revision` | The draft changed since it was read. Nothing was saved. | `tourclaim intake show`, then run the command again. |
| `intake_submitted` | The draft was submitted and is read-only. | `tourclaim claims list`. |
| `intake_incomplete` | Answers are missing. | `tourclaim intake set`. |
| `approval_required` | The traveler has not signed this revision. | `tourclaim intake sign <id> --wait`. |
| `approval_outdated` | The signature is older than 7 days, or the authorization changed. | The traveler signs again: `tourclaim intake sign <id> --wait`. |
| `evidence_conflict` | The same email or file was already added with different details. | Nothing to do, or add it under its original details. |
| `duplicate_booking` | This booking already has a claim. | `tourclaim claims list`. |

## Which drafts a key can reach

- A key from `tourclaim login` reaches every unsubmitted draft started from any `tourclaim login` on the same account, including drafts started with keys that have since expired or been revoked. Signing in again, `login --force` and `logout` do not lose drafts.
- Drafts started by other apps connected to the account, such as Muse, are not visible to the tool, and those apps do not see the tool's drafts.
- A key saved with `login --with-token` (created at `/connect/muse`) reaches only the drafts it started itself.
- Submitted claims are claims on the account: `tourclaim claims list` shows them all, including claims submitted through other apps such as Muse.

## Configuration

| Variable | Meaning |
| --- | --- |
| `TOURCLAIM_API_URL` | API base URL. `--api-url` overrides it. Plain `http://` is accepted only for localhost. |
| `TOURCLAIM_API_KEY` | A key to use instead of the stored one. It takes precedence over the stored key. A key from `/connect/muse` reaches only the drafts it started. |
| `XDG_CONFIG_HOME` | Credentials are stored in `$XDG_CONFIG_HOME/tourclaim/credentials.json` (default `~/.config/tourclaim/credentials.json`; `%APPDATA%\tourclaim\credentials.json` on Windows). |

The credentials file maps each API base URL to `{"api_key","expires_at","grant_id","scopes"}`, so staging and production keys never mix. The tool warns when the stored key expires within 3 days.

## Security

- **Keys belong to one traveler.** A key lasts 30 days and cannot be refreshed; a traveler can have at most 5 connections, and a new `tourclaim login` past that retires the oldest `tourclaim login` key. Sign in again when it expires. The traveler can revoke keys at any time at `https://app.getcopernican.com/connect/muse`, and `tourclaim logout` revokes the one in use.
- **Drafts stay with the account's CLI sign-ins.** Drafts started from `tourclaim login` stay reachable after signing in again; drafts started by other apps (such as Muse) are not visible to the tool. See [Which drafts a key can reach](#which-drafts-a-key-can-reach).
- **Stored with tight permissions.** The credentials file is written with mode 0600 inside a 0700 directory, and the tool warns if it finds the file readable by others. On Windows it lives in your user profile and relies on its permissions.
- **Never on the command line.** Keys are never accepted as arguments, where shell history and process lists would keep them. Use `tourclaim login`, pipe a key to `tourclaim login --with-token`, or set `TOURCLAIM_API_KEY` from a secret store.
- **The code is typed, never linked.** The sign-in page does not take the code from a link; the traveler types the code shown in the terminal, or relayed by an assistant they are using right now. Only continue if you, or that assistant, ran `tourclaim login` and the page shows the same code.
- **Never printed.** No command prints the key, in human or JSON output; anything shaped like a key is redacted from output.
- **Only https.** Keys are only sent over https, except to localhost for testing. Redirects are not followed.
- **A person signs.** Only the traveler can sign the claim authorization, in their own browser at the review link. The tool cannot sign and does not automate that page.
- **Sharing needs consent.** Files and emails are uploaded only after the traveler agrees, either at a prompt or through `--yes`. There is no default that shares.
- **Evidence is not instructions.** Email and file contents are stored as evidence. Neither the API nor this tool follows instructions found in them.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Python

A Python edition with the same commands, flags, output, exit codes and credentials file lives in [python/](python/). It needs Python 3.9 or newer and has no dependencies:

```sh
pip install tourclaim      # or: uvx tourclaim --help, pipx install tourclaim
tourclaim --help
```

It is also a typed library, with one method per API operation:

```python
from tourclaim import Client

client = Client()  # uses TOURCLAIM_API_KEY, else the key `tourclaim login` saved
draft = client.start_intake({"merchant_name": "Example Air"})
print(draft["missing_fields"], draft["next_questions"])
```

See [python/README.md](python/README.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). `npm test` builds the tool and runs the tests against an in-process mock of the API; `npm run mock` starts that mock for trying the tool by hand. Releases are described in [RELEASING.md](RELEASING.md).

## License

[MIT](LICENSE)
