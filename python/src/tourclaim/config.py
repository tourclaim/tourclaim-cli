"""API URL handling and the User-Agent."""

from __future__ import annotations

import platform as _platform
import sys
from typing import Mapping, Optional
from urllib.parse import urljoin, urlsplit

from .errors import UsageError

DEFAULT_API_URL = "https://app.getcopernican.com"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEFAULT_PORTS = {"https": 443, "http": 80}


def resolve_api_url(flag: Optional[str], env: Mapping[str, str]) -> str:
    """The API base URL: ``--api-url``, then ``TOURCLAIM_API_URL``, then the default."""
    if flag is not None:
        return normalize_api_url(flag, "--api-url")
    from_env = env.get("TOURCLAIM_API_URL")
    if from_env:
        return normalize_api_url(from_env, "TOURCLAIM_API_URL")
    return normalize_api_url(DEFAULT_API_URL, "default")


def normalize_api_url(raw: str, source: str = "--api-url") -> str:
    """Returns an origin plus optional path prefix, with no trailing slash.

    The result is also the key credentials are stored under, so it matches the
    Node edition exactly: ``https://app.getcopernican.com``. Plain ``http`` is
    accepted only for localhost, so a key is never sent unencrypted.
    """
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise UsageError(f"{source} is not a valid URL.") from None
    scheme = parts.scheme.lower()
    if not scheme or not parts.netloc or not host:
        raise UsageError(f"{source} is not a valid URL.")
    if scheme not in ("https", "http"):
        raise UsageError(f"{source} must be an https:// URL.")
    if parts.username is not None or parts.password is not None:
        raise UsageError(f"{source} must not contain a user name or password.")
    host = host.lower()
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise UsageError(
            f"{source} uses plain http for {host}. Keys are only sent over https (plain http is allowed for localhost)."
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc += f":{port}"
    path = parts.path.rstrip("/")
    # Accept the API root itself, which people copy from the docs.
    for suffix in ("/api/connectors/v1", "/api/connectors"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return f"{scheme}://{netloc}{path}"


def is_loopback(api_url: str) -> bool:
    host = urlsplit(api_url).hostname or ""
    return host.lower() in _LOOPBACK_HOSTS


def absolute_url(api_url: str, path_or_url: str) -> str:
    """An absolute URL for a path the API returned relative to its origin."""
    try:
        return urljoin(api_url + "/", path_or_url)
    except ValueError:
        return path_or_url


def is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def machine() -> str:
    return (_platform.machine() or "unknown").lower()


def user_agent(platform: Optional[str] = None, arch: Optional[str] = None, python_version: Optional[str] = None) -> str:
    """``tourclaim-py/<version> (<platform> <arch>; python <version>)``."""
    from . import __version__

    return "tourclaim-py/{} ({} {}; python {})".format(
        __version__,
        platform or sys.platform,
        arch or machine(),
        python_version or _platform.python_version(),
    )
