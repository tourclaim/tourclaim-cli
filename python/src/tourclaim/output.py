"""Writing to stdout and stderr, with every write redacted."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, List, Optional

from .errors import APIError, ExitCode, TourClaimError

KEY_PATTERN = re.compile(r"tc_muse_[A-Za-z0-9_-]+")


def to_json(value: Any) -> str:
    """Compact JSON, the same text JSON.stringify writes."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def redact_keys(text: str) -> str:
    """Replaces anything shaped like a TourClaim key."""
    return KEY_PATTERN.sub("tc_muse_[redacted]", text)


class Output:
    """Every write passes through redact(), so a key can never reach the
    terminal or a log even if an error message carried one."""

    def __init__(self, write_out: Callable[[str], None], write_err: Callable[[str], None], json_mode: bool) -> None:
        self._out = write_out
        self._err = write_err
        self.json = json_mode
        self._secrets: List[str] = []

    def add_secret(self, value: Optional[str]) -> None:
        """Adds a value that must never be printed (for keys without the usual prefix)."""
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)

    def redact(self, text: str) -> str:
        out = redact_keys(text)
        for secret in self._secrets:
            out = out.replace(secret, "[redacted]")
        return out

    def line(self, text: str = "") -> None:
        """A line of human output on stdout. Ignored in --json mode."""
        if not self.json:
            self._out(self.redact(text) + "\n")

    def lines(self, lines: List[str]) -> None:
        for text in lines:
            self.line(text)

    def data(self, value: Any) -> None:
        """One JSON value on its own line on stdout."""
        self._out(self.redact(to_json(value)) + "\n")

    def info(self, text: str) -> None:
        """Progress or a notice, on stderr, in both modes."""
        self._err(self.redact(text) + "\n")

    def warn(self, text: str) -> None:
        self._err(self.redact(f"warning: {text}") + "\n")

    def raw(self, text: str) -> None:
        """Raw text on stdout, used for help and the schema."""
        self._out(self.redact(text))

    def error(self, error: BaseException) -> int:
        """Reports an error and returns the exit code for it."""
        if isinstance(error, TourClaimError):
            err = error
        else:
            err = TourClaimError(str(error) or type(error).__name__, code="internal_error", exit_code=ExitCode.ERROR)
        if self.json:
            body = {"code": err.code, "message": err.message, "exit_code": err.exit_code}
            if err.status is not None:
                body["status"] = err.status
            body.update(err.extra)
            if isinstance(err, APIError) and err.detail is not None:
                body["detail"] = err.detail
            self._err(self.redact(to_json({"error": body})) + "\n")
        else:
            self._err(self.redact(f"error: {err.message}") + "\n")
            if isinstance(err, APIError) and isinstance(err.detail, list):
                for issue in err.detail:
                    issue = issue if isinstance(issue, dict) else {}
                    loc = issue.get("loc")
                    where = ".".join(str(p) for p in loc if p != "body") if isinstance(loc, list) else "request"
                    kind = f" ({issue['type']})" if issue.get("type") else ""
                    self._err(self.redact(f"  {where}: {issue.get('msg') or 'invalid'}{kind}") + "\n")
        return err.exit_code
