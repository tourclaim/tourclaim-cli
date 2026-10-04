# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First version.

### Added

- `tourclaim login` with device authorization: the traveler approves a code in their browser. `--with-token` saves an existing key from stdin or a hidden prompt; keys are never accepted as arguments.
- `tourclaim logout` revokes the key on the server and removes the stored copy.
- `tourclaim status` (alias `whoami`) shows the API mode, the signed-in account and key expiry.
- `tourclaim cards search` to find the card a booking was paid with.
- `tourclaim intake start|show|set|attach|add-email|sign|submit|delete` for the whole draft lifecycle, with optimistic revisions, idempotent creation, consent prompts for evidence, `.eml` parsing, and waiting for the traveler's signature.
- `tourclaim claims list|show` and `tourclaim schema`.
- `--json` output on every command, JSON errors on stderr, and documented exit codes.
- Credentials stored per API URL with mode 0600, `TOURCLAIM_API_KEY` and `TOURCLAIM_API_URL` overrides, and a warning when the key expires within 3 days.
- One automatic retry after a rate limit, honoring `Retry-After` up to 60 seconds.
