"""The credentials file, shared with the Node edition of tourclaim.

``$XDG_CONFIG_HOME/tourclaim/credentials.json`` (default ``~/.config``), or
``%APPDATA%\\tourclaim\\credentials.json`` on Windows. It maps each API base URL
to ``{"api_key", "expires_at", "grant_id", "scopes"}``, written as two-space
indented JSON with mode 0600 inside a 0700 directory. A key saved by either
edition works in the other.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import secrets
import stat
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .errors import TourClaimError


@dataclass
class StoredCredential:
    """What is saved per API base URL. ``repr()`` never shows the key."""

    api_key: str = field(repr=False)
    expires_at: Optional[str] = None
    grant_id: Optional[str] = None
    scopes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {"api_key": self.api_key, "expires_at": self.expires_at, "grant_id": self.grant_id, "scopes": list(self.scopes)}


def credentials_path(env: Mapping[str, str], platform: str = sys.platform, home: Optional[str] = None) -> str:
    """Where the credentials file lives for this environment and platform."""
    home = home if home is not None else os.path.expanduser("~")
    if platform == "win32":
        app_data = env.get("APPDATA") or ""
        base = app_data if app_data and ntpath.isabs(app_data) else ntpath.join(home, "AppData", "Roaming")
        return ntpath.join(base, "tourclaim", "credentials.json")
    xdg = env.get("XDG_CONFIG_HOME") or ""
    base = xdg if xdg and posixpath.isabs(xdg) else posixpath.join(home, ".config")
    return posixpath.join(base, "tourclaim", "credentials.json")


def _bad_file(path: str, what: str) -> TourClaimError:
    return TourClaimError(f"{path} {what}. Delete it and run: tourclaim login", code="bad_credentials_file")


def _read(path: str, platform: str, warn: Callable[[str], None]) -> Dict[str, StoredCredential]:
    try:
        if platform != "win32":
            info = os.stat(path)
            if stat.S_IMODE(info.st_mode) & 0o077:
                warn(f'{path} can be read by other users on this computer. Run: chmod 600 "{path}"')
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise TourClaimError(f"Could not read {path}: {error.strerror or error}") from None
    try:
        parsed = json.loads(text)
    except ValueError:
        raise _bad_file(path, "is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _bad_file(path, "has an unexpected format")
    out: Dict[str, StoredCredential] = {}
    for url, value in parsed.items():
        if not isinstance(value, dict):
            continue
        api_key = value.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            continue
        expires_at = value.get("expires_at")
        grant_id = value.get("grant_id")
        scopes = value.get("scopes")
        out[url] = StoredCredential(
            api_key=api_key,
            expires_at=expires_at if isinstance(expires_at, str) else None,
            grant_id=grant_id if isinstance(grant_id, str) else None,
            scopes=[s for s in scopes if isinstance(s, str)] if isinstance(scopes, list) else [],
        )
    return out


def _write(path: str, platform: str, data: Dict[str, StoredCredential]) -> None:
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if platform != "win32":
            os.chmod(directory, 0o700)
        if not data:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
    except OSError as error:
        raise TourClaimError(f"Could not save credentials to {path}: {error.strerror or error}") from None
    # Write a new file readable only by this user, then move it into place.
    tmp = os.path.join(directory, f".credentials.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    text = json.dumps({url: cred.to_json() for url, cred in data.items()}, indent=2, ensure_ascii=False) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        if platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as error:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise TourClaimError(f"Could not save credentials to {path}: {error.strerror or error}") from None


class CredentialStore:
    """Reads and writes the credentials file."""

    def __init__(self, path: str, platform: str = sys.platform, warn: Optional[Callable[[str], None]] = None) -> None:
        self.path = path
        self.platform = platform
        self._warn = warn or (lambda message: None)

    @classmethod
    def default(cls, warn: Optional[Callable[[str], None]] = None) -> "CredentialStore":
        """The store at the standard location for this user."""
        return cls(credentials_path(os.environ), sys.platform, warn)

    def get(self, api_url: str) -> Optional[StoredCredential]:
        return _read(self.path, self.platform, self._warn).get(api_url)

    def set(self, api_url: str, credential: StoredCredential) -> None:
        all_ = _read(self.path, self.platform, lambda message: None)
        all_[api_url] = credential
        _write(self.path, self.platform, all_)

    def remove(self, api_url: str) -> bool:
        """Removes the credential for ``api_url``. Returns True if there was one."""
        all_ = _read(self.path, self.platform, lambda message: None)
        if api_url not in all_:
            return False
        del all_[api_url]
        _write(self.path, self.platform, all_)
        return True
