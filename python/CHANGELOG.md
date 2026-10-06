# Changelog

All notable changes to the Python edition of tourclaim are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-05

### Added

- Optional Python MCP server: `pip install 'tourclaim[mcp]'` then `tourclaim mcp` (Python 3.10+). Seventeen tools reuse the existing client, including browser sign-in, all 14 connector operations, and public service discovery.
- Consent validation, structured results, credential redaction, and human-only signing in the MCP adapter. The service remains in synthetic review mode.
- Public MCP setup examples, agent discovery links, and an official MCP Registry manifest. The release workflow publishes the registry entry after the Python package succeeds.
- MCP protocol and claim-flow tests, including older clients, expired login codes, throttling, stale revisions, and local credential-save failures.

The ordinary Python CLI and library keep their Python 3.9+ support and zero runtime dependencies. The Node CLI keeps its existing command set; MCP runs through the Python edition.

## [0.1.1] - 2026-10-05

### Changed

- Package descriptions and READMEs explain TourClaim for developers, agents, platforms, and travelers without an operator referral or a particular assistant.
- Added links to the platform-neutral traveler and developer pages, explained the handoff from saved intake and selected evidence to traveler authorization, and retained the synthetic-review disclosure.
- Runtime commands, SDK behavior, authentication, credential formats, and consent requirements are unchanged.

## [0.1.0] - 2026-10-04

First version of the Python edition. Its commands, flags, JSON output, exit codes and credentials file match the Node edition.

### Added

- `tourclaim.Client`, a typed library with one method per connector API operation (14 in all, including `list_drafts`, `get_connection`, `disconnect` and `request_data_deletion`), plus `device_login()` and `wait_for_signature()`. Errors are typed exceptions carrying `status`, `code` and `message`.
- The `tourclaim` command (also `python -m tourclaim`): `login` with device authorization (the traveler types the code shown in the terminal on the sign-in page; a replaced key is retired by the server; brief server errors while waiting are retried) or `--with-token`, `logout`, `status` (alias `whoami`), `cards search`, `intake start|list|show|set|attach|add-email|sign|submit|delete`, `claims list|show` and `schema`.
- The cause of every 409 (the API's `X-TourClaim-Error` header) as the JSON error `code`, with a fallback for servers that do not send the header.
- Credentials stored per API URL in the same file and format as the Node edition, with mode 0600 in a 0700 directory (`%APPDATA%\tourclaim` on Windows); `TOURCLAIM_API_KEY` and `TOURCLAIM_API_URL` overrides; a warning when the key expires within 3 days.
- Consent prompts (or `--yes`) before any evidence is shared, attachment types detected from file contents, and `.eml` parsing with the standard library.
- One automatic retry after a rate limit, honoring `Retry-After` up to 60 seconds. No runtime dependencies; Python 3.9 or newer.
- `Client.request_data_deletion()`: asks TourClaim to delete the traveler's data; nothing is deleted until they confirm from the email TourClaim sends.
