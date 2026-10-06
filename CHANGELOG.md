# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-04

First release. The TourClaim connector API runs in review mode: claims are synthetic and nothing is filed.

### Added

- `tourclaim login` with device authorization: the tool opens the sign-in page and shows a code the traveler types there (the code is never put in a link), then prints whose account it signed in to. A sign-in that replaces a stored key sends it along so the server retires it; at the limit of 5 connections the server retires the oldest `tourclaim login` key. The tool polls once more when the code expires, so a last-second approval still signs in, and brief server errors (502, 503, 504) while waiting are retried rather than fatal. `--with-token` saves an existing key from stdin or a hidden prompt; keys are never accepted as arguments. `--force` replaces the stored key and revokes the old one.
- `tourclaim logout` revokes the key on the server and removes the stored copy.
- `tourclaim status` (alias `whoami`) shows the API mode, the signed-in account and key expiry.
- `tourclaim cards search` to find the card a booking was paid with.
- `tourclaim intake start|list|show|set|attach|add-email|sign|submit|delete` for the whole draft lifecycle, with optimistic revisions, idempotent creation, consent prompts for evidence, `.eml` parsing, and waiting for the traveler's signature. `intake list` finds open drafts started from any `tourclaim login` on the account.
- `tourclaim claims list|show`, and `tourclaim schema`, which prints the full schema the CLI uses (`/api/connectors/v1/openapi-cli.json`, or `/openapi.json` on older servers).
- `--json` output on every command, JSON errors on stderr, documented exit codes, and the API's `X-TourClaim-Error` cause as the error code for conflicts.
- Credentials stored per API URL with mode 0600, `TOURCLAIM_API_KEY` and `TOURCLAIM_API_URL` overrides, and a warning when the key expires within 3 days.
- One automatic retry after a rate limit, honoring `Retry-After` up to 60 seconds.
- Releases publish to npm from GitHub Actions with provenance through npm trusted publishing.

[0.1.0]: https://github.com/tourclaim/tourclaim-cli/releases/tag/v0.1.0
- The schema snapshot matches production 1.57.8: the assistants' schema lists eleven operations, adding `request_data_deletion`.
