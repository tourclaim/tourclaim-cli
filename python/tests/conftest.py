"""Test harness: the CLI in-process with a fake clock, terminal and browser,
against the in-process mock API."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from mock_server import MockServer  # noqa: E402

from tourclaim.cli import Deps, run  # noqa: E402
from tourclaim.credentials import CredentialStore, StoredCredential, credentials_path  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aY1EAAAAASUVORK5CYII=")
PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def parse_lines(text: str) -> List[Any]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    sleeps: List[float] = field(default_factory=list)
    opened: List[str] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)

    def json_lines(self) -> List[Any]:
        return parse_lines(self.stdout)

    def json(self) -> Any:
        lines = self.json_lines()
        return lines[-1] if lines else None

    def json_error(self) -> Dict[str, Any]:
        lines = [line for line in self.stderr.splitlines() if line.startswith("{")]
        return json.loads(lines[-1])["error"]


def base_env(home: str, api_url: Optional[str] = None) -> Dict[str, str]:
    env = {
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
    }
    if api_url:
        env["TOURCLAIM_API_URL"] = api_url
    return env


def creds_file(home: str) -> str:
    """Where the CLI keeps credentials for a test home on this platform."""
    return credentials_path(base_env(home), sys.platform, home)


def store_for(home: str) -> CredentialStore:
    return CredentialStore(creds_file(home), sys.platform)


def save_key(home: str, api_url: str, key: str, expires_at: Optional[str] = None) -> None:
    store_for(home).set(api_url, StoredCredential(key, expires_at, None, []))


def run_cli(
    argv: List[str],
    *,
    home: str,
    api_url: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    stdin: bytes = b"",
    stdin_is_tty: bool = False,
    stdout_is_tty: bool = False,
    answers: Optional[List[str]] = None,
    now: Optional[float] = None,
    open_succeeds: bool = True,
    on_sleep: Optional[Callable[[float], None]] = None,
) -> Result:
    out: List[str] = []
    err: List[str] = []
    sleeps: List[float] = []
    opened: List[str] = []
    prompts: List[str] = []
    queue = list(answers or [])
    clock = [time.time() if now is None else now]

    def prompt(question: str, hidden: bool) -> str:
        prompts.append(question)
        if not queue:
            raise AssertionError(f"unexpected prompt: {question}")
        return queue.pop(0)

    def open_url(url: str) -> bool:
        opened.append(url)
        return open_succeeds

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds
        if on_sleep:
            on_sleep(seconds)

    full_env = base_env(home, api_url)
    full_env.update(env or {})
    deps = Deps(
        env=full_env,
        platform=sys.platform,
        arch="x86_64",
        python_version="3.12.0",
        home=home,
        stdout=out.append,
        stderr=err.append,
        stdin_is_tty=stdin_is_tty,
        stdout_is_tty=stdout_is_tty,
        read_stdin=lambda: stdin,
        prompt=prompt,
        open_url=open_url,
        sleep=sleep,
        now=lambda: clock[0],
    )
    code = run(argv, deps)
    return Result(code, "".join(out), "".join(err), sleeps, opened, prompts)


class Spawned:
    """The real ``python -m tourclaim`` in a child process."""

    def __init__(self, argv: List[str], home: str, env: Dict[str, str], stdin: Optional[bytes] = None) -> None:
        child_env = {k: v for k, v in os.environ.items() if not k.startswith("TOURCLAIM_")}
        child_env.update(base_env(home))
        child_env.update({"HOME": home, "USERPROFILE": home, "PYTHONIOENCODING": "utf-8"})
        child_env.update(env)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tourclaim", *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        if stdin is not None:
            self.process.stdin.write(stdin)  # type: ignore[union-attr]
        self.process.stdin.close()  # type: ignore[union-attr]
        self._first = self.process.stdout.readline  # type: ignore[union-attr]
        self._first_line: Optional[str] = None
        self._stderr: List[bytes] = []
        self._err_thread = threading.Thread(target=lambda: self._stderr.append(self.process.stderr.read()), daemon=True)  # type: ignore[union-attr]
        self._err_thread.start()

    def first_line(self) -> str:
        if self._first_line is None:
            self._first_line = self._first().decode("utf-8")
        return self._first_line

    def done(self, timeout: float = 60) -> Result:
        first = self._first_line or ""
        rest = self.process.stdout.read().decode("utf-8")  # type: ignore[union-attr]
        code = self.process.wait(timeout=timeout)
        self._err_thread.join(timeout)
        return Result(code, first + rest, b"".join(self._stderr).decode("utf-8"))


def spawn_cli(argv: List[str], *, home: str, env: Dict[str, str], stdin: Optional[bytes] = None) -> Spawned:
    return Spawned(argv, home, env, stdin)


@pytest.fixture
def home(tmp_path) -> str:
    return str(tmp_path)


@pytest.fixture(scope="module")
def mock():
    server = MockServer()
    server.start()
    yield server
    server.stop()
