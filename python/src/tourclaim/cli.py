"""The ``tourclaim`` command.

The same commands, flags, output, exit codes and credentials file as the Node
edition. :func:`run` takes every outside dependency (environment, terminal,
browser, clock) as a :class:`Deps`, so tests can drive it in-process.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import platform as _platform
import re
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple

from .client import (
    API_PREFIX,
    FILENAME_FORMAT,
    IDEMPOTENCY_KEY_FORMAT,
    KEY_FORMAT,
    MAX_EMAIL_TEXT,
    SENT_AT_FORMAT,
    Client,
    derive_message_id,
    sentences,
)
from .config import absolute_url, is_http_url, machine, resolve_api_url, user_agent
from .credentials import CredentialStore, StoredCredential, credentials_path
from .eml import parse_eml
from .errors import (
    APIError,
    AuthenticationError,
    ConflictError,
    ExitCode,
    FileTooLargeError,
    NotFoundError,
    NotSignedInError,
    PermissionDeniedError,
    TourClaimError,
    UnavailableError,
    UnsupportedFileError,
    UsageError,
)
from .fields import build_fields
from .filetype import MAX_ATTACHMENT_BYTES, detect_content_type, format_bytes
from .format import (
    card_lines,
    claim_lines,
    format_time,
    intake_lines,
    iso_utc,
    mode_notice,
    next_step_lines,
    parse_iso,
    parse_server_time,
    relative_time,
    table,
)
from .models import DOC_TYPES, EMAIL_PROVIDERS, SCOPES
from .output import Output

PAGE_SIZE = 30
SIGN_POLL_SECONDS = 5
DEFAULT_SIGN_TIMEOUT = 900
EXPIRY_WARNING_SECONDS = 3 * 24 * 3600
DAY = 24 * 3600

# ---- outside dependencies ----


@dataclass
class Deps:
    """Everything the CLI touches outside its own code. Tests pass fakes."""

    env: Mapping[str, str]
    platform: str
    arch: str
    python_version: str
    home: str
    stdout: Callable[[str], None]
    stderr: Callable[[str], None]
    stdin_is_tty: bool
    stdout_is_tty: bool
    #: Reads all of stdin. Only used when stdin is not a terminal.
    read_stdin: Callable[[], bytes]
    #: Asks a question on stderr and reads one line from the terminal (hidden when True).
    prompt: Callable[[str, bool], str]
    #: Opens an http(s) URL in the default browser. False if it could not.
    open_url: Callable[[str], bool]
    sleep: Callable[[float], None]
    #: Unix time in seconds.
    now: Callable[[], float]


def _writer(stream: Any) -> Callable[[str], None]:
    def write(text: str) -> None:
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                buffer.write(text.encode("utf-8"))
            else:
                stream.write(text)
            stream.flush()
        except OSError as error:
            # A reader that went away (`tourclaim schema | head`); Windows reports EINVAL.
            if isinstance(error, BrokenPipeError) or error.errno in (errno.EPIPE, errno.EINVAL):
                raise BrokenPipeError(errno.EPIPE, "stdout closed") from None
            raise

    return write


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _read_stdin() -> bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    return buffer.read() if buffer is not None else sys.stdin.read().encode("utf-8")


def _cancelled() -> TourClaimError:
    return TourClaimError("Cancelled.", code="cancelled", exit_code=130)


def _prompt(question: str, hidden: bool) -> str:
    try:
        if hidden:
            return getpass.getpass(question, stream=sys.stderr)
        sys.stderr.write(question)
        sys.stderr.flush()
        return sys.stdin.readline().rstrip("\r\n")
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        raise _cancelled() from None
    except EOFError:
        return ""


def open_url(url: str, platform: str, env: Mapping[str, str]) -> bool:
    """Opens an http(s) URL in the default browser. Never runs a shell."""
    if not is_http_url(url):
        return False
    # No display (SSH session, container): do not try, or a text browser could take over the terminal.
    if platform not in ("darwin", "win32") and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - any failure means "could not open"
        return False


def real_deps() -> Deps:
    return Deps(
        env=os.environ,
        platform=sys.platform,
        arch=machine(),
        python_version=_platform.python_version(),
        home=os.path.expanduser("~"),
        stdout=_writer(sys.stdout),
        stderr=_writer(sys.stderr),
        stdin_is_tty=_isatty(sys.stdin),
        stdout_is_tty=_isatty(sys.stdout),
        read_stdin=_read_stdin,
        prompt=_prompt,
        open_url=lambda url: open_url(url, sys.platform, os.environ),
        sleep=time.sleep,
        now=time.time,
    )


# ---- context ----


class ActiveKey(NamedTuple):
    key: str
    source: str  # "env" or "stored"
    stored: Optional[StoredCredential]


_UNSET: Any = object()


class Context:
    def __init__(self, deps: Deps, out: Output, api_url: str) -> None:
        self.deps = deps
        self.out = out
        self.api_url = api_url
        self.store = CredentialStore(credentials_path(deps.env, deps.platform, deps.home), deps.platform, out.warn)
        self.user_agent = user_agent(deps.platform, deps.arch, deps.python_version)
        self._active: Any = _UNSET
        self._expiry_warned = False

    @property
    def json(self) -> bool:
        return self.out.json

    def active_key(self) -> Optional[ActiveKey]:
        """The key in effect: TOURCLAIM_API_KEY, else the credential stored for this API URL."""
        if self._active is not _UNSET:
            return self._active
        env_key = (self.deps.env.get("TOURCLAIM_API_KEY") or "").strip()
        stored = self.store.get(self.api_url)
        if stored:
            self.out.add_secret(stored.api_key)
        if env_key:
            self.out.add_secret(env_key)
            self._active = ActiveKey(env_key, "env", stored)
        elif stored:
            self._active = ActiveKey(stored.api_key, "stored", stored)
        else:
            self._active = None
        return self._active

    def forget_active_key(self) -> None:
        self._active = _UNSET

    def client(self, api_key: Optional[str] = None) -> Client:
        """A client for unauthenticated calls, or for a key given explicitly."""
        if api_key:
            self.out.add_secret(api_key)
        return Client(
            api_key,
            self.api_url,
            load_credentials=False,
            user_agent=self.user_agent,
            sleep=self.deps.sleep,
            on_notice=self.out.info,
        )

    def authed(self) -> Client:
        """A client that sends the active key. Fails with exit 3 when there is none."""
        active = self.active_key()
        if not active:
            raise self.not_signed_in()
        if active.source == "stored":
            self._warn_if_expiring(active.stored)
        return self.client(active.key)

    def not_signed_in(self) -> NotSignedInError:
        return NotSignedInError(f"Not signed in to {self.api_url}. Run: tourclaim login")

    def _warn_if_expiring(self, stored: Optional[StoredCredential]) -> None:
        if self._expiry_warned or not stored or not stored.expires_at:
            return
        expires = parse_server_time(stored.expires_at)
        if expires is None:
            return
        left = expires.timestamp() - self.deps.now()
        if left > EXPIRY_WARNING_SECONDS:
            return
        self._expiry_warned = True
        when_text = format_time(stored.expires_at)
        if left <= 0:
            self.out.warn(f"Your TourClaim key expired on {when_text}. Run: tourclaim login")
            return
        days = int(left // DAY)
        when = f"in {days} day{'' if days == 1 else 's'}" if days >= 1 else "within a day"
        self.out.warn(
            f"Your TourClaim key expires {when} ({when_text}). Keys are not renewed; to get a new one now, run: tourclaim login --force"
        )


# ---- commands ----


@dataclass
class Option:
    kind: str  # "string" or "boolean"
    description: str
    short: Optional[str] = None
    multiple: bool = False
    value: Optional[str] = None


class Args:
    def __init__(self, positionals: List[str], values: Dict[str, Any]) -> None:
        self.positionals = positionals
        self.values = values

    def str(self, name: str) -> Optional[str]:
        value = self.values.get(name)
        return value if isinstance(value, str) else None

    def strs(self, name: str) -> List[str]:
        value = self.values.get(name)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        return [value] if isinstance(value, str) else []

    def flag(self, name: str) -> bool:
        return self.values.get(name) is True

    def int_arg(self, name: str, minimum: int = 0) -> Optional[int]:
        value = self.str(name)
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]+", value) or int(value) < minimum:
            at_least = f" of at least {minimum}" if minimum > 0 else ""
            raise UsageError(f"--{name} must be a whole number{at_least}.")
        return int(value)


@dataclass
class Command:
    path: List[str]
    summary: str
    usage: str
    run: Callable[[Context, Args], Optional[int]]
    description: Optional[str] = None
    options: Dict[str, Option] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)
    aliases: List[List[str]] = field(default_factory=list)
    min_args: Optional[int] = None
    max_args: Optional[int] = None


GLOBAL_OPTIONS: Dict[str, Option] = {
    "json": Option("boolean", "Machine-readable output: one JSON value per line on stdout; errors as JSON on stderr."),
    "api-url": Option("string", "API base URL (default https://app.getcopernican.com, or TOURCLAIM_API_URL).", value="<url>"),
    "help": Option("boolean", "Show help.", short="h"),
    "version": Option("boolean", "Print the version."),
}


# ---- shared helpers for draft commands ----


def confirm(ctx: Context, question: str, yes: bool, how_to_skip: str) -> None:
    """Asks for a yes/no confirmation on the terminal, or accepts --yes.
    Without a terminal and without --yes it fails: there are no silent defaults."""
    if yes:
        return
    if not ctx.deps.stdin_is_tty:
        raise UsageError(
            f"Confirmation needed and there is no terminal to ask on. {how_to_skip}", extra={"reason": "confirmation_required"}
        )
    answer = ctx.deps.prompt(f"{question} [y/N] ", False).strip().lower()
    if answer not in ("y", "yes"):
        raise TourClaimError("Cancelled; nothing was changed.", code="cancelled")


def draft_not_found(intake_id: str, error: APIError) -> NotFoundError:
    return NotFoundError(
        404,
        f"No draft {intake_id} that this key can reach. Drafts started with tourclaim login on this account are reachable "
        "from any later tourclaim login; drafts started by other apps (such as Muse) or with a key saved by login --with-token "
        "are reachable only with the key that started them. See the drafts this key can reach with: tourclaim intake list",
        body=error.body,
    )


def get_intake(api: Client, intake_id: str) -> Dict[str, Any]:
    try:
        return api.get_intake(intake_id)  # type: ignore[return-value]
    except NotFoundError as error:
        raise draft_not_found(intake_id, error) from None


def conflict(message: str, code: str, extra: Optional[Mapping[str, Any]] = None) -> ConflictError:
    """A conflict (exit 4) with the API's stable cause as its code."""
    return ConflictError(409, message, code=code, extra=extra)


def conflict_error(api: Client, intake_id: str, error: ConflictError, tried_revision: Optional[int]) -> ConflictError:
    """After a 409 on a write to a draft, re-reads the draft and explains what
    happened, keyed on the API's X-TourClaim-Error code. Never retries."""
    current = None
    try:
        current = get_intake(api, intake_id)
    except TourClaimError:
        pass  # Keep the API's own explanation if the draft cannot be read.
    where = {"current_revision": current["revision"], "state": current["state"]} if current else {}
    reason = error.detail if isinstance(error.detail, str) else "The draft could not be changed"
    if error.code == "intake_submitted" or (current and current.get("state") == "submitted"):
        return conflict(f"Draft {intake_id} has been submitted and is read-only. Nothing was saved.", "intake_submitted", where)
    if error.code == "evidence_conflict":
        what = "message" if re.search("email", reason, re.I) else "evidence"
        return conflict(
            f"This draft already has that {what} with different details (the same message id, or the same file bytes under "
            "another name or type). Nothing was saved.",
            error.code,
            where,
        )
    if current and tried_revision is not None and current["revision"] != tried_revision:
        advice = (
            f"It is now at revision {current['revision']} (state {current['state']}); this command used revision {tried_revision}. "
            f"Nothing was saved. Check the current answers with: tourclaim intake show {intake_id}, then run the command again."
        )
    elif current:
        advice = (
            f"It is at revision {current['revision']} (state {current['state']}). Nothing was saved. "
            f"Check it with: tourclaim intake show {intake_id}"
        )
    else:
        advice = f"Nothing was saved. Check the draft with: tourclaim intake show {intake_id}"
    return conflict(sentences(reason, advice), error.code, where)


def resolve_revision(api: Client, intake_id: str, given: Optional[int]) -> Tuple[int, Optional[Dict[str, Any]]]:
    """The revision to send: --revision if given, else the draft's current one."""
    if given is not None:
        return given, None
    before = get_intake(api, intake_id)
    return before["revision"], before


def write_call(api: Client, intake_id: str, revision: int, call: Callable[[], Any]) -> Any:
    """Runs a write against a draft, explaining 404s and 409s."""
    try:
        return call()
    except NotFoundError as error:
        raise draft_not_found(intake_id, error) from None
    except ConflictError as error:
        raise conflict_error(api, intake_id, error, revision) from None


def signature_voided_notice(ctx: Context, before: Optional[Mapping[str, Any]], after: Mapping[str, Any]) -> None:
    if before and before.get("state") == "ready_to_submit" and after.get("state") not in ("ready_to_submit", "submitted"):
        ctx.out.info(
            f"This change cancelled the traveler's signature. They must review and sign again: tourclaim intake sign {after.get('id')}"
        )


def print_intake(ctx: Context, intake: Mapping[str, Any], title: str = "Draft") -> None:
    if ctx.json:
        ctx.out.data(intake)
    else:
        ctx.out.lines(intake_lines(intake, title))


def _evidence_count(intake: Mapping[str, Any]) -> str:
    n = len(intake.get("evidence") or [])
    return f"{n} evidence item{'' if n == 1 else 's'}"


def _read_file_bytes(ctx: Context, path: str) -> bytes:
    try:
        if path == "-":
            return ctx.deps.read_stdin()
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise TourClaimError(f"Could not read {path}: {error.strerror or error}") from None


REVISION_OPTION = Option(
    "string", "The revision you last saw. Default: read the draft's current revision first.", value="<n>"
)


# ---- login, logout, status ----


def _manage_keys_url(ctx: Context) -> str:
    return absolute_url(ctx.api_url, "/connect/muse")


def _parse_scopes(values: List[str]) -> List[str]:
    scopes = [s.strip() for v in values for s in v.split(",") if s.strip()]
    for scope in scopes:
        if scope not in SCOPES:
            raise UsageError(f'Unknown scope "{scope}". Scopes: {", ".join(SCOPES)}.')
    return list(dict.fromkeys(scopes))


def _key_info(ctx: Context, key: str) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    """GET /v1/key with a given key, and the mode. None when the key is rejected."""
    api = ctx.client(key)
    try:
        info = api.get_connection()
    except (AuthenticationError, PermissionDeniedError):
        return None
    return dict(info), api.mode


def _report_login(
    ctx: Context,
    info: Optional[Mapping[str, Any]],
    mode: Optional[str],
    credential: StoredCredential,
    replaced_key_id: Optional[str],
    already: bool,
    replaced_already_gone: bool = False,
) -> None:
    expires_at = (info or {}).get("expires_at") or credential.expires_at
    scopes = (info or {}).get("scopes") if info else credential.scopes
    scopes = scopes if isinstance(scopes, list) else []
    # Only keys from tourclaim login (device flow) say whose account they are.
    email = (info or {}).get("account_email") or None
    if ctx.json:
        result: Dict[str, Any] = {
            "event": "signed_in",
            "already_signed_in": already,
            "api_url": ctx.api_url,
        }
        if email:
            result["account_email"] = email
        result.update(
            {
                "key": {"id": (info or {}).get("id") or credential.grant_id, "expires_at": expires_at, "scopes": scopes},
                "grant_id": credential.grant_id,
                "mode": mode,
                "credentials_path": ctx.store.path,
                "replaced_key_id": replaced_key_id,
            }
        )
        ctx.out.data(result)
        return
    expiry = f"key expires {format_time(expires_at)}, {relative_time(expires_at, ctx.deps.now())}"
    who = f" as {email}" if email else ""
    ctx.out.line(f"Already signed in{who} ({expiry})." if already else f"Signed in{who} ({expiry}).")
    if scopes:
        ctx.out.line(f"Permissions: {', '.join(scopes)}")
    if not already:
        ctx.out.line(f"Key saved to {ctx.store.path} (readable only by you).")
    if replaced_key_id:
        ctx.out.line(
            f"The previous key {replaced_key_id} had already stopped working."
            if replaced_already_gone
            else f"Revoked the previous key {replaced_key_id}."
        )
    notice = mode_notice(mode)
    if notice:
        ctx.out.line(notice[0].upper() + notice[1:] + ".")
    if already:
        ctx.out.line("To replace this key, run: tourclaim login --force")


def _device_flow(ctx: Context, scopes: List[str], no_browser: bool) -> Dict[str, Any]:
    api = ctx.client()
    code = api.request_device_code(scopes or None, client_name=ctx.user_agent)
    # The code is typed by the traveler, never carried in a link: a link with
    # the code in it is what a phishing message would send.
    if ctx.json:
        ctx.out.data(
            {
                "event": "device_code",
                "user_code": code["user_code"],
                "verification_uri": code["verification_uri"],
                "expires_in": code["expires_in"],
                "interval": code.get("interval", 5),
            }
        )
    else:
        ctx.out.lines(
            [
                "",
                "To connect this computer to your TourClaim account, open this page:",
                "",
                f"  {code['verification_uri']}",
                "",
                f"Enter this code on that page: {code['user_code']}",
                "",
                "Type the code yourself. Only approve if you started this sign-in in this terminal.",
            ]
        )
    opened = False
    if ctx.deps.stdout_is_tty and not no_browser:
        opened = ctx.deps.open_url(code["verification_uri"])
    if not ctx.json:
        ctx.out.line("Opened the page in your browser." if opened else "")
    minutes = max(1, int(code["expires_in"] / 60 + 0.5))
    ctx.out.info(f"Waiting for approval (the code expires in {minutes} minute{'' if minutes == 1 else 's'}). Press Ctrl-C to cancel.")
    return dict(api.wait_for_device_token(code, sleep=ctx.deps.sleep, now=ctx.deps.now))


def _read_key(ctx: Context) -> str:
    if ctx.deps.stdin_is_tty:
        raw = ctx.deps.prompt("Paste the TourClaim API key (input is hidden): ", True)
    else:
        raw = ctx.deps.read_stdin().decode("utf-8", errors="replace")
    key = raw.strip()
    ctx.out.add_secret(key)
    if not key:
        raise UsageError("No key was given. Pipe it in: tourclaim login --with-token < key.txt")
    if not KEY_FORMAT.fullmatch(key):
        raise UsageError(
            "That does not look like a TourClaim key. Keys start with tc_muse_ and contain only letters, digits, - and _."
        )
    return key


def run_login(ctx: Context, args: Args) -> int:
    if args.positionals:
        raise UsageError(
            "tourclaim login takes no arguments. Never put a key on the command line (shell history and process lists keep it); "
            "pipe it instead: tourclaim login --with-token < key.txt"
        )
    with_token = args.flag("with-token")
    force = args.flag("force")
    scopes = _parse_scopes(args.strs("scope"))
    if with_token and scopes:
        raise UsageError("--scope applies to the browser sign-in; a pasted key keeps the permissions it was created with.")
    if ctx.deps.env.get("TOURCLAIM_API_KEY"):
        ctx.out.warn("TOURCLAIM_API_KEY is set. It overrides the stored key for every command until you unset it.")

    existing = ctx.store.get(ctx.api_url)
    if existing:
        ctx.out.add_secret(existing.api_key)
    existing_check = _key_info(ctx, existing.api_key) if existing else None
    if existing and existing_check and not force:
        if with_token:
            email = existing_check[0].get("account_email")
            as_who = f" as {email}" if email else ""
            raise UsageError(
                f"Already signed in to {ctx.api_url}{as_who}. Pass --force to replace the stored key; the old key is revoked."
            )
        _report_login(ctx, existing_check[0], existing_check[1], existing, None, True)
        return ExitCode.OK

    confirmed: Optional[Tuple[Dict[str, Any], Optional[str]]] = None
    if with_token:
        key = _read_key(ctx)
        confirmed = _key_info(ctx, key)
        if not confirmed:
            raise TourClaimError(
                "The API rejected that key (expired, revoked or mistyped). Nothing was saved.",
                code="unauthorized",
                exit_code=ExitCode.AUTH,
            )
        info = confirmed[0]
        credential = StoredCredential(key, info.get("expires_at"), info.get("id"), list(info.get("scopes") or []))
    else:
        token = _device_flow(ctx, scopes, args.flag("no-browser"))
        ctx.out.add_secret(token["api_key"])
        credential = StoredCredential(token["api_key"], token.get("expires_at"), token.get("grant_id"), list(token.get("scopes") or []))

    ctx.store.set(ctx.api_url, credential)
    ctx.forget_active_key()

    replaced_key_id = None
    replaced_already_gone = False
    if existing and existing_check and existing.api_key != credential.api_key:
        try:
            ctx.client(existing.api_key).disconnect()
            replaced_key_id = existing_check[0].get("id")
        except AuthenticationError:
            # At the key limit the server retires the oldest tourclaim login key itself.
            replaced_key_id = existing_check[0].get("id")
            replaced_already_gone = True
        except TourClaimError as error:
            ctx.out.warn(f"Could not revoke the previous key ({error.message}). Revoke it at {_manage_keys_url(ctx)}")

    if not confirmed:
        try:
            confirmed = _key_info(ctx, credential.api_key)
        except TourClaimError as error:
            ctx.out.warn(f"Saved the key, but could not confirm it: {error.message}")
    _report_login(
        ctx, confirmed[0] if confirmed else None, confirmed[1] if confirmed else None, credential, replaced_key_id, False, replaced_already_gone
    )
    return ExitCode.OK


def run_logout(ctx: Context, args: Args) -> int:
    active = ctx.active_key()
    if not active:
        if ctx.json:
            ctx.out.data({"api_url": ctx.api_url, "revoked": False, "already_invalid": False, "credential_removed": False, "key_source": None})
        else:
            ctx.out.line(f"Not signed in to {ctx.api_url}; nothing to do.")
        return ExitCode.OK
    revoked = False
    already_invalid = False
    try:
        ctx.client(active.key).disconnect()
        revoked = True
    except AuthenticationError:
        already_invalid = True
    except TourClaimError as error:
        error.message = sentences(
            error.message, f"The key was not revoked and is still stored. Try again, or revoke it at {_manage_keys_url(ctx)}"
        )
        raise
    removed = False
    if active.stored and (active.source == "stored" or active.stored.api_key == active.key):
        removed = ctx.store.remove(ctx.api_url)
    if ctx.json:
        ctx.out.data(
            {
                "api_url": ctx.api_url,
                "revoked": revoked,
                "already_invalid": already_invalid,
                "credential_removed": removed,
                "key_source": active.source,
            }
        )
    else:
        ctx.out.line(f"Revoked the key for {ctx.api_url}." if revoked else "The key was already expired or revoked.")
        if removed:
            ctx.out.line(f"Removed it from {ctx.store.path}.")
    if active.source == "env":
        ctx.out.info("TOURCLAIM_API_KEY is still set in this environment; unset it.")
        if active.stored and not removed:
            ctx.out.info(
                f"A different key is still stored for {ctx.api_url}. Unset TOURCLAIM_API_KEY and run tourclaim logout again to revoke it too."
            )
    return ExitCode.OK


def run_status(ctx: Context, args: Args) -> int:
    info = dict(ctx.client().connector_info() or {})
    active = ctx.active_key()
    key: Optional[Dict[str, Any]] = None
    key_problem: Optional[str] = None
    if active:
        try:
            key = dict(ctx.authed().get_connection())
        except (AuthenticationError, PermissionDeniedError):
            key_problem = "rejected"
        except UnavailableError:
            key_problem = "connector_unavailable"
    signed_in = key is not None
    connection_url = absolute_url(ctx.api_url, info.get("connection_url") or "/connect/muse")
    enabled = bool(info.get("enabled"))
    if ctx.json:
        ctx.out.data(
            {
                "api_url": ctx.api_url,
                "mode": info.get("mode"),
                "enabled": info.get("enabled"),
                "connector": {
                    "name": info.get("name"),
                    "version": info.get("version"),
                    "enabled": info.get("enabled"),
                    "mode": info.get("mode"),
                    "openapi_url": absolute_url(ctx.api_url, info.get("openapi_url") or f"{API_PREFIX}/openapi.json"),
                    "connection_url": connection_url,
                    "documentation_url": absolute_url(ctx.api_url, info.get("documentation_url") or "/muse/developers"),
                    "cli_login_url": absolute_url(ctx.api_url, info.get("cli_login_url") or "/connect/cli"),
                },
                "signed_in": signed_in,
                **({"account_email": key["account_email"]} if key and key.get("account_email") else {}),
                "key": {"id": key.get("id"), "expires_at": key.get("expires_at"), "scopes": key.get("scopes")} if key else None,
                "key_source": active.source if active else None,
                "key_problem": key_problem,
                "credentials_path": ctx.store.path,
            }
        )
    else:
        notice = mode_notice(info.get("mode"))
        if notice:
            ctx.out.lines([notice.upper(), ""])

        def pad(label: str) -> str:
            return label.ljust(14)

        ctx.out.line(pad("API:") + ctx.api_url)
        ctx.out.line(pad("Connector:") + f"{'enabled' if enabled else 'disabled'} ({info.get('name')} {info.get('version')})")
        ctx.out.line(pad("Mode:") + str(info.get("mode")))
        if key:
            email = key.get("account_email")
            ctx.out.line(pad("Signed in:") + "yes" + (f", as {email}" if email else ""))
            expires = key.get("expires_at")
            ctx.out.line(pad("Key:") + f"{key.get('id')}, expires {format_time(expires)} ({relative_time(expires, ctx.deps.now())})")
            ctx.out.line(pad("Permissions:") + (", ".join(key.get("scopes") or []) or "none"))
            ctx.out.line(pad("Key source:") + ("TOURCLAIM_API_KEY" if active and active.source == "env" else ctx.store.path))
        elif key_problem == "rejected":
            ctx.out.line(pad("Signed in:") + "no: the key was rejected (expired or revoked). Run: tourclaim login")
        elif key_problem:
            ctx.out.line(pad("Signed in:") + "unknown: the connector is unavailable")
        else:
            ctx.out.line(pad("Signed in:") + "no. Run: tourclaim login")
        ctx.out.line(pad("Manage keys:") + connection_url)
    if not enabled:
        return ExitCode.UNAVAILABLE
    if signed_in:
        return ExitCode.OK
    return ExitCode.UNAVAILABLE if key_problem == "connector_unavailable" else ExitCode.AUTH


# ---- cards, claims, schema ----


def run_cards_search(ctx: Context, args: Args) -> int:
    query = " ".join(args.positionals).strip()
    if not query:
        raise UsageError("Give part of the card's product name, such as: tourclaim cards search sapphire")
    if len(query) > 100:
        raise UsageError("The search text can be at most 100 characters.")
    if re.search(r"[0-9]{12,}", re.sub(r"[\s-]", "", query)):
        raise UsageError("That looks like a card number. Search by the card's product name instead; never share card numbers.")
    cards = ctx.authed().search_cards(query)
    if ctx.json:
        ctx.out.data(cards)
        return ExitCode.OK
    if not cards:
        ctx.out.line(f'No cards matched "{query}". Try a shorter part of the name.')
        return ExitCode.OK
    ctx.out.lines(card_lines(cards))  # type: ignore[arg-type]
    ctx.out.lines(
        [
            "",
            "Finding a card here does not mean it covers the loss; Copernican reviews coverage.",
            f"Use the id as card_product_id, for example: tourclaim intake set <draft-id> card_product_id={cards[0].get('id', '<id>')}",
        ]
    )
    return ExitCode.OK


def run_claims_list(ctx: Context, args: Args) -> int:
    offset = args.int_arg("offset") or 0
    claims = ctx.authed().list_claims(offset)
    if ctx.json:
        ctx.out.data(claims)
        return ExitCode.OK
    if not claims:
        ctx.out.line("No more claims." if offset else "No submitted claims yet.")
        return ExitCode.OK
    notice = mode_notice(claims[0].get("mode"))
    if notice and claims[0].get("mode") == "review":
        ctx.out.lines([notice, ""])
    rows = [[str(c.get("id")), str(c.get("status")), format_time(c.get("updated_at")), str(c.get("next_action"))] for c in claims]
    ctx.out.lines(table(["CLAIM", "STATUS", "UPDATED", "NEXT"], rows))
    if len(claims) >= PAGE_SIZE:
        ctx.out.lines(["", f"More claims may exist: tourclaim claims list --offset {offset + len(claims)}"])
    return ExitCode.OK


def run_claims_show(ctx: Context, args: Args) -> int:
    claim_id = args.positionals[0]
    api = ctx.authed()
    try:
        claim = api.get_claim(claim_id)
    except NotFoundError as error:
        raise NotFoundError(
            404, f"No claim {claim_id} for this traveler. List claims with: tourclaim claims list", body=error.body
        ) from None
    if ctx.json:
        ctx.out.data(claim)
    else:
        ctx.out.lines(claim_lines(claim, ctx.deps.now()))
    return ExitCode.OK


def run_schema(ctx: Context, args: Args) -> int:
    data = ctx.client().schema()
    if ctx.json:
        ctx.out.data(data)
    else:
        ctx.out.raw(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return ExitCode.OK


# ---- intake ----


def run_intake_start(ctx: Context, args: Args) -> int:
    fields = build_fields(file=args.str("fields-file"), pairs=args.strs("set"), read_stdin=ctx.deps.read_stdin)
    key = args.str("idempotency-key")
    if key is None:
        key = str(uuid.uuid4())
    if not IDEMPOTENCY_KEY_FORMAT.fullmatch(key):
        raise UsageError("--idempotency-key must be 8 to 100 letters, digits, - or _.")
    api = ctx.authed()
    try:
        intake = api.start_intake(fields, idempotency_key=key)  # type: ignore[arg-type]
    except TourClaimError as error:
        error.extra["idempotency_key"] = key
        if error.code in ("idempotency_key_reused", "idempotency_key_other_connection"):
            error.message = sentences(error.message, "Use a new --idempotency-key (or leave it out) to start a different draft")
        elif error.code == "concurrent_request" or not isinstance(error, APIError) or (error.status or 0) >= 429:
            error.message = (
                f"{error.message} The draft may or may not have been created. Retry with the same fields and "
                f"--idempotency-key {key} so a duplicate is not created."
            )
        raise
    if ctx.json:
        ctx.out.data(intake)
        return ExitCode.OK
    ctx.out.lines(intake_lines(intake, "Started draft"))
    ctx.out.lines(["", "Find it again any time with: tourclaim intake list"])
    return ExitCode.OK


def run_intake_list(ctx: Context, args: Args) -> int:
    offset = args.int_arg("offset") or 0
    drafts = ctx.authed().list_drafts(offset)
    if ctx.json:
        ctx.out.data(drafts)
        return ExitCode.OK
    if not drafts:
        ctx.out.line("No more drafts." if offset else "No open drafts. Start one with: tourclaim intake start")
        return ExitCode.OK
    rows = []
    for draft in drafts:
        fields = draft.get("fields") or {}
        missing = draft.get("missing_fields") or []
        rows.append(
            [
                str(draft.get("id")),
                str(draft.get("state")),
                str(fields.get("merchant_name") or "-"),
                str(fields.get("booking_ref") or "-"),
                str(len(missing)) if missing else "-",
            ]
        )
    ctx.out.lines(table(["DRAFT", "STATE", "MERCHANT", "BOOKING", "MISSING"], rows))
    if len(drafts) >= PAGE_SIZE:
        ctx.out.lines(["", f"More drafts may exist: tourclaim intake list --offset {offset + len(drafts)}"])
    ctx.out.lines(["", "Show one with: tourclaim intake show <draft>"])
    return ExitCode.OK


def run_intake_show(ctx: Context, args: Args) -> int:
    print_intake(ctx, get_intake(ctx.authed(), args.positionals[0]))
    return ExitCode.OK


def run_intake_set(ctx: Context, args: Args) -> int:
    intake_id, pairs = args.positionals[0], args.positionals[1:]
    fields = build_fields(file=args.str("fields-file"), pairs=pairs, clears=args.strs("clear"), read_stdin=ctx.deps.read_stdin)
    if not fields:
        raise UsageError("Nothing to change. Give <field>=<value> pairs, --clear <field> or --fields-file.")
    api = ctx.authed()
    revision, before = resolve_revision(api, intake_id, args.int_arg("revision", 1))
    if before and before.get("state") == "submitted":
        raise conflict(f"Draft {intake_id} has been submitted and is read-only. Nothing was saved.", "intake_submitted", {"state": "submitted"})
    intake = write_call(api, intake_id, revision, lambda: api.update_intake(intake_id, fields, expected_revision=revision))  # type: ignore[arg-type]
    print_intake(ctx, intake, "Saved draft")
    signature_voided_notice(ctx, before, intake)
    return ExitCode.OK


def run_intake_delete(ctx: Context, args: Args) -> int:
    intake_id = args.positionals[0]
    confirm(
        ctx,
        f"Delete draft {intake_id} and every answer, email and file saved with it? This cannot be undone.",
        args.flag("yes"),
        "Pass --yes once the traveler has confirmed they want this draft deleted.",
    )
    api = ctx.authed()
    try:
        api.delete_draft(intake_id)
    except NotFoundError as error:
        raise draft_not_found(intake_id, error) from None
    except ConflictError as error:
        if error.code == "intake_submitted":
            raise conflict(
                f"Draft {intake_id} was submitted as a claim and cannot be deleted here. To request deletion, see "
                f"{absolute_url(ctx.api_url, '/muse/data-deletion')}",
                error.code,
            ) from None
        raise conflict_error(api, intake_id, error, None) from None
    if ctx.json:
        ctx.out.data({"id": intake_id, "deleted": True})
    else:
        ctx.out.line(f"Deleted draft {intake_id}.")
    return ExitCode.OK


def run_intake_attach(ctx: Context, args: Args) -> int:
    intake_id, path = args.positionals[0], args.positionals[1]
    doc_type = args.str("type")
    if not doc_type:
        raise UsageError(f"--type is required: {', '.join(DOC_TYPES)}.")
    if doc_type not in DOC_TYPES:
        raise UsageError(f"--type must be one of: {', '.join(DOC_TYPES)}.")
    try:
        if not os.path.isfile(path):
            if os.path.exists(path):
                raise TourClaimError(f"{path} is not a file.")
            raise TourClaimError(f"Could not read {path}: no such file.")
        size = os.path.getsize(path)
    except OSError as error:
        raise TourClaimError(f"Could not read {path}: {error.strerror or error}.") from None
    if size == 0:
        raise TourClaimError(f"{path} is empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise FileTooLargeError(f"{path} is {format_bytes(size)}; the limit is 5 MiB. Ask the traveler for a smaller copy.")
    data = _read_file_bytes(ctx, path)
    content_type = detect_content_type(data)
    if not content_type:
        raise UnsupportedFileError(
            f"{path} is not a PDF, JPEG or PNG (checked by its contents, not its name). Convert it first, or give the traveler "
            "the draft's evidence upload link so they can add it in their browser."
        )
    filename = os.path.basename(path)
    if not FILENAME_FORMAT.fullmatch(filename) or len(filename) > 200:
        raise UsageError("The file name must be 1 to 200 characters with no control characters. Rename the file and try again.")

    confirm(
        ctx,
        f"Share {filename} ({content_type}, {format_bytes(size)}) with TourClaim as {doc_type} evidence for draft {intake_id}?\n"
        "Answer yes only if the traveler agreed to share this file.",
        args.flag("yes"),
        "Pass --yes only after the traveler has agreed to share this file.",
    )

    api = ctx.authed()
    revision, before = resolve_revision(api, intake_id, args.int_arg("revision", 1))
    intake = write_call(
        api,
        intake_id,
        revision,
        lambda: api.add_attachment(
            intake_id,
            expected_revision=revision,
            filename=filename,
            content=data,
            doc_type=doc_type,
            content_type=content_type,
            user_authorized_sharing=True,
        ),
    )
    if ctx.json:
        ctx.out.data(intake)
    else:
        ctx.out.line(
            f"Attached {filename} as {doc_type} evidence to draft {intake.get('id')} (revision {intake.get('revision')}, {_evidence_count(intake)})."
        )
        ctx.out.lines(next_step_lines(intake))
    signature_voided_notice(ctx, before, intake)
    return ExitCode.OK


def run_intake_add_email(ctx: Context, args: Args) -> int:
    intake_id = args.positionals[0]
    eml_path = args.str("eml")
    text_path = args.str("text-file")
    if bool(eml_path) == bool(text_path):
        raise UsageError("Give exactly one of --eml <file> or --text-file <file>.")

    subject = args.str("subject")
    sender = args.str("from")
    sent_at = args.str("sent-at")
    message_id = args.str("message-id")
    if eml_path:
        parsed = parse_eml(_read_file_bytes(ctx, eml_path))
        if parsed.text is None:
            raise TourClaimError(
                f"{eml_path} has no text/plain part. Save the message as plain text and use --text-file.", code="unsupported_email"
            )
        text = parsed.text
        if subject is None:
            subject = parsed.subject or ""
        if sender is None:
            sender = parsed.sender
        if sent_at is None:
            sent_at = parsed.date
        if message_id is None:
            message_id = parsed.message_id
    else:
        raw = _read_file_bytes(ctx, text_path or "")
        text = raw.decode("utf-8", errors="replace")
        if text.startswith("﻿"):
            text = text[1:]
        if subject is None:
            raise UsageError("--subject is required with --text-file.")
        if not sender:
            raise UsageError("--from is required with --text-file.")
    if not sender:
        raise UsageError("The message has no From header; pass --from.")
    provider = args.str("provider") or "user"
    if provider not in EMAIL_PROVIDERS:
        raise UsageError(f"--provider must be one of: {', '.join(EMAIL_PROVIDERS)}.")
    if sent_at is not None:
        moment = parse_iso(sent_at, default_utc=False) if SENT_AT_FORMAT.fullmatch(sent_at) else None
        if moment is None:
            raise UsageError("--sent-at must be an ISO 8601 date and time with a zone, such as 2026-03-03T14:05:00Z.")
        sent_at = iso_utc(moment)
    if not text.strip():
        raise TourClaimError("The message text is empty.")
    length = len(text)
    if length > MAX_EMAIL_TEXT:
        raise TourClaimError(
            f"The message text is {length:,} characters; the API accepts at most 30,000. Share the one relevant message, "
            "unmodified; do not summarize it."
        )
    if len(subject) > 500:
        raise UsageError("The subject is longer than 500 characters.")
    if len(sender) > 320:
        raise UsageError("The From address is longer than 320 characters.")
    if message_id is None:
        message_id = derive_message_id(sender, subject, sent_at, text)
    if not message_id or len(message_id) > 255:
        raise UsageError("--message-id must be 1 to 255 characters.")

    confirm(
        ctx,
        "\n".join(
            [
                f"Share this email with TourClaim as evidence for draft {intake_id}?",
                f"  From:    {sender}",
                f"  Subject: {subject}",
                f"  Sent:    {sent_at or 'not given'}",
                f"  Text:    {length} characters",
                "Answer yes only if the traveler agreed to share this message.",
            ]
        ),
        args.flag("yes"),
        "Pass --yes only after the traveler has seen which message will be shared and agreed.",
    )

    api = ctx.authed()
    revision, before = resolve_revision(api, intake_id, args.int_arg("revision", 1))
    final_subject, final_sender, final_sent_at, final_message_id = subject, sender, sent_at, message_id
    intake = write_call(
        api,
        intake_id,
        revision,
        lambda: api.add_email(
            intake_id,
            expected_revision=revision,
            provider=provider,
            message_id=final_message_id,
            subject=final_subject,
            sender=final_sender,
            sent_at=final_sent_at,
            text=text,
            user_authorized_sharing=True,
        ),
    )
    if ctx.json:
        ctx.out.data(intake)
    else:
        ctx.out.line(f"Added the email to draft {intake.get('id')} (revision {intake.get('revision')}, {_evidence_count(intake)}).")
        ctx.out.lines(next_step_lines(intake))
    signature_voided_notice(ctx, before, intake)
    return ExitCode.OK


def _incomplete(intake_id: str, intake: Mapping[str, Any]) -> ConflictError:
    missing = intake.get("missing_fields") or []
    return conflict(
        f"Draft {intake_id} is not complete; it still needs: {', '.join(missing)}. Save those with tourclaim intake set first.",
        "intake_incomplete",
        {"state": intake.get("state"), "current_revision": intake.get("revision"), "missing_fields": missing},
    )


def _not_signed(intake_id: str, intake: Mapping[str, Any], code: str = "approval_required", reason: Optional[str] = None) -> ConflictError:
    if code == "approval_outdated":
        why = (
            "The traveler's signature is out of date (it is older than 7 days, or the authorization changed); "
            "they must review and sign again."
        )
    else:
        why = sentences(reason, f"The traveler has not signed revision {intake.get('revision')} of draft {intake_id}.")
    review_url = intake.get("review_url")
    return conflict(
        f"{why} Only they can sign, in their own browser, at: {review_url or '(no link returned)'}. "
        f"Wait for the signature with: tourclaim intake sign {intake_id} --wait",
        code,
        {"review_url": review_url, "current_revision": intake.get("revision"), "state": intake.get("state")},
    )


def _submit_conflict(api: Client, intake_id: str, error: ConflictError) -> ConflictError:
    """Explains a refused submission by its X-TourClaim-Error code, after re-reading the draft."""
    current = None
    try:
        current = get_intake(api, intake_id)
    except TourClaimError:
        pass  # Fall back to the API's own explanation.
    reason = error.detail if isinstance(error.detail, str) else "The draft cannot be submitted"
    where = {"state": current["state"], "current_revision": current["revision"]} if current else {}
    code = error.code
    if code in ("approval_required", "approval_outdated") and current:
        return _not_signed(intake_id, current, code, reason)
    if code == "intake_incomplete" and current:
        return _incomplete(intake_id, current)
    if code == "duplicate_booking":
        return conflict(
            sentences(
                reason,
                "One claim per booking: this booking reference already has a claim for this traveler. See tourclaim claims list",
            ),
            code,
            where,
        )
    if code == "stale_revision":
        now_at = f" (it is now at revision {current['revision']}, state {current['state']})" if current else ""
        return conflict(sentences(reason, f"The draft changed{now_at}. Check it with: tourclaim intake show {intake_id}"), code, where)
    return conflict(sentences(reason, f"Check it with: tourclaim intake show {intake_id}"), code, where)


def run_intake_sign(ctx: Context, args: Args) -> int:
    intake_id = args.positionals[0]
    timeout = args.int_arg("timeout", 1) or DEFAULT_SIGN_TIMEOUT
    wait = args.flag("wait")
    api = ctx.authed()
    intake = get_intake(api, intake_id)

    state = intake.get("state")
    if state == "collecting":
        raise _incomplete(intake_id, intake)
    if state in ("ready_to_submit", "submitted"):
        if ctx.json:
            ctx.out.data(intake)
        elif state == "submitted":
            ctx.out.line(f"Draft {intake_id} was already signed and submitted as claim {intake.get('claim_id')}.")
        else:
            ctx.out.lines([f"The traveler has already signed revision {intake.get('revision')}.", f"Next: tourclaim intake submit {intake_id}"])
        return ExitCode.OK

    review_url = intake.get("review_url")
    if not is_http_url(review_url):
        raise TourClaimError("The API did not return a review link for this draft.", code="bad_response")
    if not ctx.json:
        ctx.out.lines(
            [
                "Only the traveler can sign. They review this draft and sign the authorization in their own browser;",
                "this tool cannot sign for them.",
                "",
                f"  Review and sign: {review_url}",
                "",
            ]
        )
    opened = False
    if ctx.deps.stdout_is_tty and not args.flag("no-browser"):
        opened = ctx.deps.open_url(review_url)
    if opened and not ctx.json:
        ctx.out.line("Opened the link in your browser.")

    if not wait:
        if ctx.json:
            ctx.out.data(intake)
        else:
            ctx.out.line(
                f"When they have signed, run: tourclaim intake submit {intake_id}   (or wait with: tourclaim intake sign {intake_id} --wait)"
            )
        return ExitCode.OK

    if ctx.json:
        ctx.out.data({"event": "waiting_for_signature", "intake_id": intake_id, "review_url": review_url, "timeout_seconds": timeout})
    minutes = int(timeout / 60 + 0.5)
    span = f"{minutes} minute{'' if minutes == 1 else 's'}" if minutes >= 1 else f"{timeout} seconds"
    ctx.out.info(
        f"Waiting for the traveler to sign (checking every 5 seconds for up to {span}). Ctrl-C stops waiting; the link stays valid."
    )
    deadline = ctx.deps.now() + timeout
    while ctx.deps.now() + SIGN_POLL_SECONDS <= deadline:
        ctx.deps.sleep(SIGN_POLL_SECONDS)
        current = get_intake(api, intake_id)
        if current.get("state") in ("ready_to_submit", "submitted"):
            if ctx.json:
                ctx.out.data(current)
            else:
                ctx.out.lines([f"Signed: the traveler approved revision {current.get('revision')}."] + next_step_lines(current))
            return ExitCode.OK
        if current.get("state") == "collecting":
            missing = current.get("missing_fields") or []
            raise conflict(
                f"Draft {intake_id} changed while waiting and needs answers again: {', '.join(missing)}.",
                "intake_incomplete",
                {"state": current.get("state"), "current_revision": current.get("revision"), "missing_fields": missing},
            )
    raise TourClaimError(
        f"The traveler has not signed after {timeout} seconds. The review link stays valid: {review_url}. "
        "Run this command again to keep waiting.",
        code="timeout",
        extra={"review_url": review_url},
    )


def run_intake_submit(ctx: Context, args: Args) -> int:
    intake_id = args.positionals[0]
    api = ctx.authed()
    revision = args.int_arg("revision", 1)
    if revision is None:
        intake = get_intake(api, intake_id)
        # Answers missing: say which. Anything else (unsigned, or a signature
        # that is out of date) the API names more precisely than the state does.
        if intake.get("state") == "collecting":
            raise _incomplete(intake_id, intake)
        revision = intake["revision"]
    final_revision = revision
    try:
        claim = api.submit(intake_id, expected_revision=final_revision)
    except NotFoundError as error:
        raise draft_not_found(intake_id, error) from None
    except ConflictError as error:
        raise _submit_conflict(api, intake_id, error) from None
    if ctx.json:
        ctx.out.data(claim)
        return ExitCode.OK
    ctx.out.lines([f"Submitted draft {intake_id} as claim {claim.get('id')}.", ""] + claim_lines(claim, ctx.deps.now()) + [""])
    ctx.out.lines(
        [
            "Copernican reviews the claim next. Nothing here decides coverage or promises reimbursement.",
            f"Check its status any time: tourclaim claims show {claim.get('id')}",
        ]
    )
    return ExitCode.OK


# ---- the command table ----

COMMANDS: List[Command] = [
    Command(
        path=["login"],
        summary="Connect this computer to a traveler's TourClaim account",
        usage="tourclaim login [--no-browser] [--scope <scope>]... [--force]\n       tourclaim login --with-token [--force] < key.txt",
        description="\n".join(
            [
                "Opens the sign-in page; the traveler types the code shown here and approves in their own browser. Then saves a key",
                "for this API URL. At the limit of 5 connections, the server retires the account's oldest tourclaim login key.",
                "The key belongs to one traveler, lasts 30 days and cannot be refreshed; sign in again when it expires.",
                "With --json, the first line carries the page URL and the code to show the traveler; the last line is the result.",
                "",
                "--with-token saves a key the traveler already created at /connect/muse. It reads the key from stdin",
                "(piped) or a hidden prompt. Keys are never accepted as command-line arguments.",
            ]
        ),
        options={
            "with-token": Option("boolean", "Read an existing key from stdin or a hidden prompt instead of the browser flow."),
            "no-browser": Option("boolean", "Do not open the browser; just print the URL and code."),
            "scope": Option(
                "string",
                f"Ask only for these permissions (repeat or comma-separate): {', '.join(SCOPES)}. Default: all.",
                multiple=True,
                value="<scope>",
            ),
            "force": Option(
                "boolean",
                "Replace a valid stored key with a new one and revoke the old key (a traveler can hold at most 5). Drafts stay reachable.",
            ),
        },
        examples=["tourclaim login", "tourclaim login --json --no-browser", "tourclaim login --with-token < key.txt"],
        run=run_login,
    ),
    Command(
        path=["logout"],
        summary="Revoke the key and remove it from this computer",
        usage="tourclaim logout",
        description=(
            "Revokes the key in use on the server, then deletes the stored copy (even if the server already considers it invalid).\n"
            "Unsubmitted drafts started with tourclaim login stay reachable the next time you run tourclaim login; submitted claims are not affected."
        ),
        max_args=0,
        run=run_logout,
    ),
    Command(
        path=["status"],
        aliases=[["whoami"]],
        summary="Show the API mode and who is signed in",
        usage="tourclaim status",
        description=(
            "Shows whether the connector is enabled, whether it runs in review mode (synthetic claims) or live, and the signed-in traveler and key expiry.\n"
            "Exits 0 when signed in, 3 when not signed in or the key is rejected, 7 when the connector is disabled."
        ),
        max_args=0,
        run=run_status,
    ),
    Command(
        path=["cards", "search"],
        summary="Find the card a booking was paid with",
        usage="tourclaim cards search <query...>",
        description="\n".join(
            [
                'Searches the card catalog by product name, for example "Sapphire" or "Venture" (at most 30 matches).',
                "Use the id as card_product_id. A card appearing here does not mean it covers the loss.",
                "Never type or send a card number, expiry date or security code.",
            ]
        ),
        min_args=1,
        examples=["tourclaim cards search sapphire preferred", "tourclaim cards search venture --json"],
        run=run_cards_search,
    ),
    Command(
        path=["intake", "start"],
        summary="Start a claim draft",
        usage="tourclaim intake start [--set <field>=<value>]... [--fields-file <file>] [--idempotency-key <key>]",
        description="\n".join(
            [
                "Starts a draft with whatever the traveler has already said; every field is optional here.",
                "The result lists what is still missing and up to three questions to ask next.",
                "A random Idempotency-Key is generated unless you pass one. If the command fails or times out,",
                "rerun it with the same --idempotency-key and the same fields to get the same draft instead of a second one.",
                "Check tourclaim intake list first: the traveler may already have a draft for this trip.",
                "",
                "Fields: merchant_name, booking_ref, trip_date (YYYY-MM-DD), booking_amount, refunded_amount (decimal, USD),",
                "currency (USD), card_product_id (from cards search), reason_category, narrative, cancellation_policy,",
                "other_insurance (YES, NO, UNSURE), other_insurance_details, medical.<answer>. See tourclaim intake set --help.",
            ]
        ),
        options={
            "set": Option("string", "A field to save. Repeatable. Use field:=<json> for a raw JSON value.", multiple=True, value="<field>=<value>"),
            "fields-file": Option("string", "A JSON object of fields (- reads stdin). --set values override it.", value="<file>"),
            "idempotency-key": Option("string", "8 to 100 letters, digits, - or _. Default: a random UUID.", value="<key>"),
        },
        max_args=0,
        examples=[
            'tourclaim intake start --set merchant_name="Example Air" --set booking_ref=EXA-482913 --set reason_category=weather',
            "tourclaim intake start --fields-file answers.json --json",
        ],
        run=run_intake_start,
    ),
    Command(
        path=["intake", "list"],
        summary="List drafts not yet submitted",
        usage="tourclaim intake list [--offset <n>]",
        description="\n".join(
            [
                "Lists the traveler's drafts that were never submitted, most recently changed first, 30 at a time.",
                "A key from tourclaim login reaches the drafts started from any tourclaim login on the same account, so",
                "signing in again or a key running out does not lose a draft. Drafts started by other apps (such as Muse),",
                "or with a key saved by login --with-token, are listed only with the key that started them.",
                "Submitted drafts are claims: see tourclaim claims list.",
            ]
        ),
        options={"offset": Option("string", "How many drafts to skip (default 0).", value="<n>")},
        max_args=0,
        run=run_intake_list,
    ),
    Command(
        path=["intake", "show"],
        summary="Show a draft, what it still needs and its state",
        usage="tourclaim intake show <id>",
        min_args=1,
        max_args=1,
        run=run_intake_show,
    ),
    Command(
        path=["intake", "set"],
        summary="Save answers to a draft",
        usage="tourclaim intake set <id> [<field>=<value>]... [<field>:=<json>]... [--clear <field>]... [--fields-file <file>]",
        description="\n".join(
            [
                "Saves only the fields given; everything else is unchanged. --clear sets a field to null (clears it).",
                "field=value is typed by the schema: amounts like 250.00, dates like 2026-03-04, true/false answers, and",
                "enum values in any case (weather becomes WEATHER). field:=<json> sends a JSON value as is.",
                "Medical answers are addressed as medical.<answer>, for example medical.provider_seen=true.",
                "",
                "The command reads the current revision and sends it as expected_revision. If the draft changed in between,",
                "nothing is saved: the command exits 4 and says what the draft looks like now. It does not retry on its own.",
                "Any change after the traveler signed cancels the signature; they must sign again.",
                "",
                "Fields: merchant_name, booking_ref, trip_date, booking_amount, refunded_amount, currency, card_product_id,",
                "reason_category (ILLNESS, INJURY, DEATH_IN_FAMILY, MILITARY_DEPLOYMENT, WEATHER, AIRLINE_CANCELLATION, OTHER),",
                "narrative, cancellation_policy, other_insurance (YES, NO, UNSURE), other_insurance_details,",
                "medical.symptom_onset_date, medical.provider_seen, medical.provider_details, medical.prior_condition,",
                "medical.prior_condition_details, medical.stability_60day, medical.stability_60day_details.",
            ]
        ),
        options={
            "clear": Option("string", "Clear a field (send null). Repeatable.", multiple=True, value="<field>"),
            "fields-file": Option("string", "A JSON object of fields (- reads stdin). Pairs override it.", value="<file>"),
            "revision": REVISION_OPTION,
        },
        min_args=1,
        examples=[
            "tourclaim intake set 1148bfae-... trip_date=2026-03-04 booking_amount=250.00 refunded_amount=50.00 currency=USD",
            "tourclaim intake set 1148bfae-... card_product_id=412 other_insurance=no",
            'tourclaim intake set 1148bfae-... narrative="The harbor closed for a storm warning and the tour was cancelled."',
            "tourclaim intake set 1148bfae-... medical.provider_seen=true --clear medical.provider_details",
        ],
        run=run_intake_set,
    ),
    Command(
        path=["intake", "attach"],
        summary="Attach a PDF, JPEG or PNG the traveler chose to share",
        usage="tourclaim intake attach <id> <file> --type <receipt|itinerary|medical_note|other> [--yes]",
        description="\n".join(
            [
                "Uploads one file as evidence: a receipt, itinerary, or a doctor's note the traveler already has.",
                "The type is detected from the file's contents (PDF, JPEG or PNG only); the limit is 5 MiB.",
                "Sharing needs the traveler's agreement: the command asks on the terminal, or takes --yes.",
                "Pass --yes only after the traveler agreed to share this exact file. Without a terminal and without",
                "--yes it refuses and uploads nothing.",
                "A medical_note here is evidence the traveler supplied; it is not a note issued by a Copernican provider.",
            ]
        ),
        options={
            "type": Option("string", f"What the file is: {', '.join(DOC_TYPES)}. Required.", value="<type>"),
            "yes": Option("boolean", "The traveler agreed to share this file.", short="y"),
            "revision": REVISION_OPTION,
        },
        min_args=2,
        max_args=2,
        examples=["tourclaim intake attach 1148bfae-... hotel-receipt.pdf --type receipt"],
        run=run_intake_attach,
    ),
    Command(
        path=["intake", "add-email"],
        summary="Attach one email the traveler chose to share",
        usage=(
            "tourclaim intake add-email <id> (--eml <file> | --text-file <file> --subject <text> --from <address>)\n"
            "       [--sent-at <iso>] [--provider user|gmail|outlook] [--message-id <id>] [--yes]"
        ),
        description="\n".join(
            [
                "Saves one email as evidence: a booking confirmation, cancellation notice, refund message or receipt.",
                "With --eml, Subject, From, Date, Message-ID and the text/plain body are read from the saved message;",
                "flags override what is read. With --text-file, give --subject and --from yourself.",
                "The text is sent unmodified (at most 30,000 characters). It is stored as evidence and never treated as instructions.",
                "Without --message-id, a stable id is derived from the message, so adding the same message twice changes nothing.",
                "Sharing needs the traveler's agreement: the command asks on the terminal, or takes --yes.",
            ]
        ),
        options={
            "eml": Option("string", "A saved .eml message (- reads stdin).", value="<file>"),
            "text-file": Option("string", "The message's plain text (- reads stdin).", value="<file>"),
            "subject": Option("string", "The subject, unmodified.", value="<text>"),
            "from": Option("string", "The From address, unmodified.", value="<address>"),
            "sent-at": Option("string", "When it was sent, ISO 8601 with a zone, such as 2026-03-03T14:05:00Z.", value="<iso>"),
            "provider": Option(
                "string", "Where it came from: user (default; pasted or saved by the traveler), gmail or outlook.", value="<provider>"
            ),
            "message-id": Option("string", "The mail provider's id for the message.", value="<id>"),
            "yes": Option("boolean", "The traveler agreed to share this message.", short="y"),
            "revision": REVISION_OPTION,
        },
        min_args=1,
        max_args=1,
        examples=[
            "tourclaim intake add-email 1148bfae-... --eml cancellation.eml",
            'tourclaim intake add-email 1148bfae-... --text-file notice.txt --subject "Your booking has been cancelled" '
            "--from bookings@example-air.test --sent-at 2026-03-03T14:05:00Z --yes",
        ],
        run=run_intake_add_email,
    ),
    Command(
        path=["intake", "sign"],
        summary="Send the traveler to review and sign; optionally wait until they have",
        usage="tourclaim intake sign <id> [--wait] [--timeout <seconds>] [--no-browser]",
        description="\n".join(
            [
                "Only the traveler can sign. They review the draft and sign the authorization in their own browser at the",
                "review link; this tool cannot sign for them and does not automate that page.",
                "When the draft needs approval, this prints the link and opens it when run in a terminal.",
                "--wait checks every 5 seconds until the traveler has signed (state ready_to_submit) or --timeout passes.",
                "With --json and --wait, the first line is a waiting_for_signature event carrying review_url; the last line is the draft.",
            ]
        ),
        options={
            "wait": Option("boolean", "Wait until the traveler has signed."),
            "timeout": Option("string", f"How long --wait waits (default {DEFAULT_SIGN_TIMEOUT}).", value="<seconds>"),
            "no-browser": Option("boolean", "Do not open the review link in a browser."),
        },
        min_args=1,
        max_args=1,
        run=run_intake_sign,
    ),
    Command(
        path=["intake", "submit"],
        summary="Submit a draft the traveler has signed",
        usage="tourclaim intake submit <id> [--revision <n>]",
        description="\n".join(
            [
                "Hands a complete, signed draft to Copernican as a claim. Safe to retry: a repeated call returns the same claim.",
                "This does not file anything with an insurer, charge a fee or promise reimbursement.",
                "Refused (exit 4) when the traveler has not signed the current revision, the signature is older than 7 days,",
                "the draft is incomplete, or the booking already has a claim.",
            ]
        ),
        options={
            "revision": Option(
                "string", "The revision the traveler signed. Default: the draft's current revision.", value="<n>"
            )
        },
        min_args=1,
        max_args=1,
        run=run_intake_submit,
    ),
    Command(
        path=["intake", "delete"],
        summary="Delete a draft that was never submitted",
        usage="tourclaim intake delete <id> [--yes]",
        description=(
            "Permanently deletes the draft with every answer, email and file saved against it. Asks for confirmation unless --yes is given.\n"
            "A submitted draft is a claim and cannot be deleted here."
        ),
        options={"yes": Option("boolean", "Skip the confirmation (only after the traveler confirmed).", short="y")},
        min_args=1,
        max_args=1,
        run=run_intake_delete,
    ),
    Command(
        path=["claims", "list"],
        summary="List the traveler's submitted claims",
        usage="tourclaim claims list [--offset <n>]",
        description="Lists claims submitted through the connector, newest first, 30 at a time. Drafts that were never submitted are not listed.",
        options={"offset": Option("string", "How many claims to skip (default 0).", value="<n>")},
        max_args=0,
        run=run_claims_list,
    ),
    Command(
        path=["claims", "show"],
        summary="Show where one claim stands and what happens next",
        usage="tourclaim claims show <claim-id>",
        description="Relay next_action to the traveler as written. A status is not a coverage decision unless it says so.",
        min_args=1,
        max_args=1,
        run=run_claims_show,
    ),
    Command(
        path=["schema"],
        summary="Print the live OpenAPI schema",
        usage="tourclaim schema [--json]",
        description=f"Fetches {API_PREFIX}/openapi.json (no sign-in needed) and prints it: indented by default, one line with --json.",
        max_args=0,
        run=run_schema,
    ),
]

GROUPS = {
    "cards": "Look up the card a booking was paid with",
    "intake": "Start, fill in, sign and submit a claim draft",
    "claims": "Check submitted claims",
}


# ---- help ----


def _option_lines(options: Mapping[str, Option]) -> List[str]:
    entries = []
    for name, spec in options.items():
        left = (f"-{spec.short}, " if spec.short else "    ") + f"--{name}" + (f" {spec.value}" if spec.value else "")
        entries.append((left, spec.description))
    width = max(len(left) for left, _ in entries) + 2
    return [f"  {left.ljust(width)}{desc}" for left, desc in entries]


def _command_name(cmd: Command, strip: int = 0) -> str:
    alias = f" ({', '.join(' '.join(a) for a in cmd.aliases)})" if cmd.aliases else ""
    return " ".join(cmd.path[strip:]) + alias


def _command_list(commands: List[Command], strip: int = 0, extra: Optional[List[Tuple[str, str]]] = None) -> List[str]:
    rows = [(_command_name(c, strip), c.summary) for c in commands] + list(extra or [])
    width = max(len(name) for name, _ in rows) + 2
    return [f"  {name.ljust(width)}{summary}" for name, summary in rows]


def top_help() -> str:
    from . import __version__

    return "\n".join(
        [
            f"tourclaim {__version__}: file a trip cancellation claim against the travel benefits of the credit card it was booked with,",
            "through TourClaim by Copernican.",
            "",
            "Usage: tourclaim <command> [options]",
            "",
            "Commands:",
            *_command_list(COMMANDS, 0, [("help [command]", "Show help for a command")]),
            "",
            "Global options:",
            *_option_lines(GLOBAL_OPTIONS),
            "",
            "Environment:",
            "  TOURCLAIM_API_URL     API base URL (default https://app.getcopernican.com)",
            "  TOURCLAIM_API_KEY     A key to use instead of the stored one (it takes precedence)",
            "  XDG_CONFIG_HOME       Credentials go in $XDG_CONFIG_HOME/tourclaim (default ~/.config; %APPDATA% on Windows)",
            "",
            "Exit codes:",
            "  0 ok  1 error  2 usage  3 not signed in or key rejected  4 conflict",
            "  5 rate limited  6 not found  7 connector disabled or unavailable",
            "",
            "Run tourclaim status to see whether the API is in review mode (synthetic claims, nothing filed) or live.",
            "Only the traveler can sign a claim authorization, in their own browser; this tool never signs.",
            "Run tourclaim <command> --help for details.",
            "",
        ]
    )


def group_help(group: str) -> str:
    commands = [c for c in COMMANDS if c.path[0] == group]
    return "\n".join(
        [
            f"Usage: tourclaim {group} <command> [options]",
            "",
            f"{GROUPS.get(group, '')}.",
            "",
            "Commands:",
            *_command_list(commands, 1),
            "",
            f"Run tourclaim {group} <command> --help for details.",
            "",
        ]
    )


def command_help(cmd: Command) -> str:
    lines = [f"Usage: {cmd.usage}", "", f"{cmd.summary}."]
    if cmd.description:
        lines += ["", cmd.description]
    if cmd.options:
        lines += ["", "Options:", *_option_lines(cmd.options)]
    lines += ["", "Global options:", *_option_lines(GLOBAL_OPTIONS)]
    if cmd.aliases:
        lines += ["", f"Alias: tourclaim {', '.join(' '.join(a) for a in cmd.aliases)}"]
    if cmd.examples:
        lines += ["", "Examples:", *[f"  {e}" for e in cmd.examples]]
    lines.append("")
    return "\n".join(lines)


def _help_for(target: List[str]) -> str:
    if not target:
        return top_help()
    first = target[0]
    second = target[1] if len(target) > 1 else None
    for cmd in COMMANDS:
        if cmd.path == target[: len(cmd.path)] or any(" ".join(a) == first for a in cmd.aliases):
            if len(cmd.path) == 1 or second is not None:
                return command_help(cmd)
            break
    if first in GROUPS:
        return group_help(first)
    raise UsageError(f'No help for "{" ".join(target)}". Run: tourclaim --help')


# ---- routing and parsing ----


class _Route(NamedTuple):
    command: Optional[Command]
    group: Optional[str]
    rest: List[str]
    help_target: Optional[List[str]]


def _route(argv: List[str]) -> _Route:
    """Finds the command words, skipping options anywhere before them."""
    words: List[Tuple[str, int]] = []
    i = 0
    while i < len(argv) and len(words) < 2:
        token = argv[i]
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            if token == "--api-url":
                i += 1
            i += 1
            continue
        words.append((token, i))
        i += 1

    def without(count: int) -> List[str]:
        skip = {index for _, index in words[:count]}
        return [t for index, t in enumerate(argv) if index not in skip]

    if not words:
        return _Route(None, None, argv, None)
    first = words[0][0]
    if first == "help":
        rest = without(1)
        return _Route(None, None, rest, [t for t in rest if not t.startswith("-")])
    for cmd in COMMANDS:
        if (len(cmd.path) == 1 and cmd.path[0] == first) or any(len(a) == 1 and a[0] == first for a in cmd.aliases):
            return _Route(cmd, None, without(1), None)
    if first in GROUPS:
        if len(words) < 2:
            return _Route(None, first, without(1), None)
        second = words[1][0]
        for cmd in COMMANDS:
            if cmd.path[0] == first and len(cmd.path) > 1 and cmd.path[1] == second:
                return _Route(cmd, first, without(2), None)
        raise UsageError(f'Unknown command "{first} {second}". Run: tourclaim {first} --help')
    raise UsageError(f'Unknown command "{first}". Run: tourclaim --help')


class _Parser(argparse.ArgumentParser):
    """argparse, with errors raised as UsageError instead of exiting."""

    def __init__(self, path: List[str]) -> None:
        super().__init__(prog="tourclaim " + " ".join(path), add_help=False, allow_abbrev=False)
        self._path = path

    def error(self, message: str):  # type: ignore[override]
        unknown = re.match(r"unrecognized arguments: (.*)", message)
        if unknown:
            # Name only the option, never its value (it could be a key).
            names = list(dict.fromkeys(tok.split("=", 1)[0] for tok in unknown.group(1).split()))
            message = f"Unknown option{'s' if len(names) > 1 else ''} {', '.join(repr(n) for n in names)}"
        message = message[:1].upper() + message[1:]
        raise UsageError(f"{message.rstrip('.')}. Run: tourclaim {' '.join(self._path)} --help")

    def exit(self, status: int = 0, message: Optional[str] = None):  # type: ignore[override]
        raise UsageError((message or "Invalid arguments.").strip())


def _parse(cmd: Command, rest: List[str]) -> Args:
    parser = _Parser(cmd.path)
    specs = dict(GLOBAL_OPTIONS)
    specs.update(cmd.options)
    for name, spec in specs.items():
        flags = [f"--{name}"] + ([f"-{spec.short}"] if spec.short else [])
        dest = "opt_" + name.replace("-", "_")
        if spec.kind == "boolean":
            parser.add_argument(*flags, dest=dest, action="store_true", default=False)
        elif spec.multiple:
            parser.add_argument(*flags, dest=dest, action="append", default=None)
        else:
            parser.add_argument(*flags, dest=dest, default=None)
    parser.add_argument("positionals", nargs="*")
    namespace = parser.parse_intermixed_args(rest)
    values = {name: getattr(namespace, "opt_" + name.replace("-", "_")) for name in specs}
    return Args(list(namespace.positionals or []), values)


def _print_version(out: Output) -> None:
    from . import __version__

    if out.json:
        out.data({"name": "tourclaim", "version": __version__})
    else:
        out.raw(f"{__version__}\n")


def run(argv: List[str], deps: Deps) -> int:
    """Runs the CLI and returns the exit code. Never calls sys.exit."""
    head = argv[: argv.index("--")] if "--" in argv else argv
    out = Output(deps.stdout, deps.stderr, "--json" in head)
    try:
        route = _route(argv)
        if route.help_target is not None:
            out.raw(_help_for(route.help_target))
            return ExitCode.OK
        if route.command is None:
            if "--version" in head and not route.group:
                _print_version(out)
                return ExitCode.OK
            out.raw(group_help(route.group) if route.group else top_help())
            return ExitCode.OK
        cmd = route.command
        args = _parse(cmd, route.rest)
        if args.flag("version"):
            _print_version(out)
            return ExitCode.OK
        if args.flag("help"):
            out.raw(command_help(cmd))
            return ExitCode.OK
        n = len(args.positionals)
        if (cmd.min_args is not None and n < cmd.min_args) or (cmd.max_args is not None and n > cmd.max_args):
            problem = "Missing arguments" if n < (cmd.min_args or 0) else "Too many arguments"
            raise UsageError(f"{problem}. Usage: {cmd.usage}")
        api_url = resolve_api_url(args.str("api-url"), deps.env)
        ctx = Context(deps, out, api_url)
        code = cmd.run(ctx, args)
        return ExitCode.OK if code is None else int(code)
    except BrokenPipeError:
        raise
    except Exception as error:  # noqa: BLE001 - every failure is reported, with its exit code
        return out.error(error)


def main(argv: Optional[List[str]] = None) -> int:
    """The console script: ``tourclaim``."""
    try:
        return run(list(sys.argv[1:] if argv is None else argv), real_deps())
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # `tourclaim schema | head` closes stdout early; that is not an error.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0
