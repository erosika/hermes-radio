"""Synchronous client for the radio daemon.

Uses blocking sockets, never the caller's event loop, so it is safe from any
thread and from inside a running asyncio loop.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import paths

STALE_AFTER = 3.0
PING_TIMEOUT = 1.0

_CONNECT_ERRORS = (
    FileNotFoundError,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


class RadioError(Exception):
    """The daemon replied with ok=false."""


class RadioUnavailable(Exception):
    """No daemon answers and none could be started."""


def radio_dir() -> Path:
    return paths.radio_dir()


def _inactive_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "pid": None,
        "updated_at": 0.0,
        "active": False,
        "paused": False,
        "muted": False,
        "source_mode": "",
        "station_name": "",
        "title": "",
        "artist": "",
        "decade": 0,
        "country": "",
        "mood": "",
        "position": None,
        "duration": None,
        "volume": 0,
        "recording": False,
        "recording_path": None,
        "levels": [],
        "meter_active": False,
    }


def _request(method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """One connection, one request line, one reply line."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    buf = bytearray()
    try:
        sock.connect(str(paths.socket_path()))
        line = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
        sock.sendall(line.encode("utf-8"))
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    if not buf:
        raise ConnectionResetError("daemon closed the connection without replying")
    reply = json.loads(buf.decode("utf-8"))
    if not isinstance(reply, dict):
        raise ValueError(f"daemon reply is not an object: {reply!r}")
    return reply


def daemon_running() -> bool:
    """True when the socket connects and ping succeeds."""
    try:
        reply = _request("ping", {}, PING_TIMEOUT)
    except (OSError, ValueError):
        return False
    return reply.get("ok") is True


def _read_launcher() -> Dict[str, Any]:
    try:
        data = json.loads(paths.launcher_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_daemon(timeout: float = 8.0) -> None:
    """Start the daemon when no daemon answers. Raises RadioUnavailable."""
    if daemon_running():
        return
    if not shutil.which("mpv"):
        raise RadioUnavailable("mpv not found in PATH. Install: brew install mpv")

    spec = _read_launcher()
    python = spec.get("python") or sys.executable
    script = spec.get("script") or str(paths.daemon_script())
    hermes_root = spec.get("hermes_root")

    env = os.environ.copy()
    env["HERMES_HOME"] = str(paths.hermes_home())
    if hermes_root:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(hermes_root) + (os.pathsep + existing if existing else "")

    log_path = radio_dir() / "radio.log"
    try:
        with open(log_path, "ab") as log_file:
            proc = subprocess.Popen(
                [python, script],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
    except OSError as e:
        raise RadioUnavailable(f"could not start the radio daemon: {e}") from e

    deadline = time.monotonic() + timeout
    while True:
        if daemon_running():
            return
        code = proc.poll()
        if code is not None:
            # Another client may have won the race to start it.
            if daemon_running():
                return
            raise RadioUnavailable(f"radio daemon exited with code {code}; see {log_path}")
        if time.monotonic() >= deadline:
            raise RadioUnavailable(f"radio daemon did not answer within {timeout:g}s; see {log_path}")
        time.sleep(0.1)


def call(method: str, *, timeout: float = 30.0, start: bool = True, **params) -> Any:
    """Send one request. Raises RadioError on ok=false, RadioUnavailable when no daemon."""
    try:
        reply = _request(method, params, timeout)
    except _CONNECT_ERRORS as e:
        if not start:
            raise RadioUnavailable(f"radio daemon is not running: {e}") from e
        ensure_daemon()
        try:
            reply = _request(method, params, timeout)
        except _CONNECT_ERRORS as retry_err:
            raise RadioUnavailable(f"radio daemon did not answer: {retry_err}") from retry_err
    except TimeoutError as e:
        raise RadioError(f"{method} timed out after {timeout:g}s") from e
    except ValueError as e:
        raise RadioError(f"bad reply from radio daemon: {e}") from e

    if not reply.get("ok"):
        raise RadioError(str(reply.get("error") or "unknown error"))
    return reply.get("result")


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state() -> Dict[str, Any]:
    """The state.json dict, or an inactive-shaped dict when missing, unparsable, or stale."""
    try:
        data = json.loads(paths.state_path().read_text())
    except (OSError, ValueError):
        return _inactive_state()
    if not isinstance(data, dict):
        return _inactive_state()

    state = {**_inactive_state(), **data}
    if not isinstance(state.get("levels"), list):
        state["levels"] = []
    try:
        age = time.time() - float(state.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        age = float("inf")
    if age > STALE_AFTER and not _pid_alive(state.get("pid")):
        return _inactive_state()
    return state


def write_launcher(python: str, script: str, hermes_root: Optional[str]) -> None:
    """Write daemon.json so non-Python clients can spawn the daemon."""
    path = paths.launcher_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"python": python, "script": script, "hermes_root": hermes_root}, indent=2) + "\n")
    os.replace(tmp, path)
