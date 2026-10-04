# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-04

First release. The TourClaim connector API runs in review mode: claims are synthetic and nothing is filed.

### Added

- `tourclaim login` with device authorization: the traveler approves a code in their browser, and the tool prints whose account it signed in to. `--with-token` saves an existing key from stdin or a hidden prompt; keys are never accepted as arguments. `--force` replaces the stored key and revokes the old one.
- `tourclaim logout` revokes the key on the server and removes the stored copy.
- `tourclaim status` (alias `whoami`) shows the API mode, the signed-in account and key expiry.
- `tourclaim cards search` to find the card a booking was paid with.
- `tourclaim intake start|list|show|set|attach|add-email|sign|submit|delete` for the whole draft lifecycle, with optimistic revisions, idempotent creation, consent prompts for evidence, `.eml` parsing, and waiting for the traveler's signature. `intake list` finds open drafts started from any `tourclaim login` on the account.
- `tourclaim claims list|show` and `tourclaim schema`.
- `--json` output on every command, JSON errors on stderr, documented exit codes, and the API's `X-TourClaim-Error` cause as the error code for conflicts.
- Credentials stored per API URL with mode 0600, `TOURCLAIM_API_KEY` and `TOURCLAIM_API_URL` overrides, and a warning when the key expires within 3 days.
- One automatic retry after a rate limit, honoring `Retry-After` up to 60 seconds.
- Releases publish to npm from GitHub Actions with provenance through npm trusted publishing.

[0.1.0]: https://github.com/tourclaim/tourclaim-cli/releases/tag/v0.1.0
