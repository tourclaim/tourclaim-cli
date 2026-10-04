# Changelog

All notable changes to the Python edition of tourclaim are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-04

First version of the Python edition. Its commands, flags, JSON output, exit codes and credentials file match the Node edition.

### Added

- `tourclaim.Client`, a typed library with one method per connector API operation (13 in all, including `list_drafts`, `get_connection` and `disconnect`), plus `device_login()` and `wait_for_signature()`. Errors are typed exceptions carrying `status`, `code` and `message`.
- The `tourclaim` command (also `python -m tourclaim`): `login` with device authorization (the traveler types the code shown in the terminal on the sign-in page; a replaced key is retired by the server; brief server errors while waiting are retried) or `--with-token`, `logout`, `status` (alias `whoami`), `cards search`, `intake start|list|show|set|attach|add-email|sign|submit|delete`, `claims list|show` and `schema`.
- The cause of every 409 (the API's `X-TourClaim-Error` header) as the JSON error `code`, with a fallback for servers that do not send the header.
- Credentials stored per API URL in the same file and format as the Node edition, with mode 0600 in a 0700 directory (`%APPDATA%\tourclaim` on Windows); `TOURCLAIM_API_KEY` and `TOURCLAIM_API_URL` overrides; a warning when the key expires within 3 days.
- Consent prompts (or `--yes`) before any evidence is shared, attachment types detected from file contents, and `.eml` parsing with the standard library.
- One automatic retry after a rate limit, honoring `Retry-After` up to 60 seconds. No runtime dependencies; Python 3.9 or newer.
