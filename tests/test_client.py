import asyncio
import json
import os
import socket
import subprocess
import threading
import time

import pytest

from hermes_radio import client, daemon, paths
from hermes_radio.client import RadioError, RadioUnavailable

STATE_KEYS = set(client._inactive_state())


# ---------------------------------------------------------------- read_state


def test_read_state_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = client.read_state()
    assert set(state) == STATE_KEYS
    assert state["active"] is False
    assert state["levels"] == []
    assert state["version"] == 1


def test_read_state_unparsable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    paths.state_path().write_text("{not json")
    state = client.read_state()
    assert set(state) == STATE_KEYS
    assert state["active"] is False


def test_read_state_stale_and_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dead_pid = 2 ** 22 - 7
    paths.state_path().write_text(json.dumps({
        "version": 1, "pid": dead_pid, "updated_at": time.time() - 60,
        "active": True, "station_name": "Ghost FM", "levels": [0.5],
    }))
    state = client.read_state()
    assert set(state) == STATE_KEYS
    assert state["active"] is False
    assert state["station_name"] == ""
    assert state["levels"] == []


def test_read_state_old_but_pid_alive_is_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    paths.state_path().write_text(json.dumps({
        "version": 1, "pid": os.getpid(), "updated_at": time.time() - 60,
        "active": True, "station_name": "Still Here", "levels": [0.2, 0.3],
    }))
    state = client.read_state()
    assert set(state) == STATE_KEYS
    assert state["active"] is True
    assert state["station_name"] == "Still Here"
    assert state["levels"] == [0.2, 0.3]


def test_read_state_fresh_fills_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    paths.state_path().write_text(json.dumps({"version": 1, "pid": os.getpid(), "updated_at": time.time(), "active": True}))
    state = client.read_state()
    assert set(state) == STATE_KEYS
    assert state["active"] is True
    assert state["levels"] == []


# ------------------------------------------------------------ ensure_daemon


def test_ensure_daemon_without_mpv_does_not_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(client, "daemon_running", lambda: False)
    monkeypatch.setattr(client.shutil, "which", lambda name: None)

    def no_spawn(*args, **kwargs):
        raise AssertionError("Popen must not be called without mpv")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    with pytest.raises(RadioUnavailable, match=r"mpv not found in PATH\. Install: brew install mpv"):
        client.ensure_daemon(timeout=0.5)


def test_write_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client.write_launcher("/py", "/script.py", "/hermes")
    assert json.loads(paths.launcher_path().read_text()) == {"python": "/py", "script": "/script.py", "hermes_root": "/hermes"}


def test_call_without_daemon_and_start_false(short_home):
    with pytest.raises(RadioUnavailable):
        client.call("ping", start=False)
    assert client.daemon_running() is False


# ------------------------------------------------ daemon round trip, no mpv


class FakeRadio:
    def __init__(self):
        self._cb = None
        self.volume = 50
        self.stopped = False
        self.active = False

    def set_state_callback(self, cb):
        self._cb = cb

    def snapshot(self):
        return {
            "version": 1, "pid": os.getpid(), "updated_at": time.time(), "active": self.active,
            "paused": False, "muted": False, "source_mode": "", "station_name": "Fake FM",
            "title": "", "artist": "", "decade": 0, "country": "", "mood": "",
            "position": None, "duration": None, "volume": self.volume, "recording": False,
            "recording_path": None, "levels": [0.1, 0.2], "meter_active": False,
        }

    async def status(self):
        return self.snapshot()

    async def set_volume(self, level):
        self.volume = int(level)
        if self._cb:
            self._cb()
        return f"Volume: {self.volume}%"

    async def play_stream(self, url, station_name=""):
        self.active = True
        if self._cb:
            self._cb()
        return f"Tuned to {station_name or url}"

    async def stop(self):
        self.stopped = True
        self.active = False
        return "Radio stopped"


@pytest.fixture
def running_daemon(short_home):
    made = []

    def factory():
        radio = FakeRadio()
        made.append(radio)
        return radio

    d = daemon.RadioDaemon(radio_factory=factory)
    errors = []

    def runner():
        try:
            asyncio.run(d.run())
        except BaseException as e:
            errors.append(e)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not client.daemon_running():
        assert thread.is_alive(), f"daemon thread died: {errors}"
        assert time.monotonic() < deadline, "daemon did not answer ping"
        time.sleep(0.05)
    yield d, made, thread
    if thread.is_alive():
        d.request_stop()
        thread.join(5)
    assert not errors, errors


def raw_line(payload: bytes, timeout: float = 5.0) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(paths.socket_path()))
        sock.sendall(payload)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    return json.loads(buf)


def test_daemon_round_trip(running_daemon):
    d, made, thread = running_daemon
    radio = made[0]

    assert paths.pid_path().read_text().strip() == str(os.getpid())
    assert paths.socket_path().exists()

    pong = client.call("ping", start=False)
    assert pong == {"pid": os.getpid(), "active": False}

    status = client.call("status", start=False)
    assert status["station_name"] == "Fake FM"
    assert status["levels"] == [0.1, 0.2]

    assert client.call("set_volume", level=70, start=False) == "Volume: 70%"
    assert radio.volume == 70
    state = client.read_state()
    assert state["volume"] == 70
    assert state["pid"] == os.getpid()

    assert client.call("play_stream", url="http://x", station_name="X", start=False) == "Tuned to X"
    assert client.call("ping", start=False)["active"] is True
    time.sleep(0.4)
    assert client.read_state()["active"] is True

    with pytest.raises(RadioError, match="unknown method: bogus"):
        client.call("bogus", start=False)

    with pytest.raises(RadioError, match="unknown method: snapshot"):
        client.call("snapshot", start=False)

    with pytest.raises(RadioError):
        client.call("set_volume", nonsense=1, start=False)

    reply = raw_line(b"this is not json\n")
    assert reply["ok"] is False and "error" in reply

    reply = raw_line(b"[1, 2, 3]\n")
    assert reply["ok"] is False

    reply = raw_line(json.dumps({"id": 9, "params": {}}).encode() + b"\n")
    assert reply == {"id": 9, "ok": False, "error": "request has no method"}

    reply = raw_line(json.dumps({"id": 7, "method": "ping", "params": []}).encode() + b"\n")
    assert reply["id"] == 7 and reply["ok"] is False

    assert client.call("stop", start=False) == "Radio stopped"
    thread.join(5)
    assert not thread.is_alive()
    assert radio.stopped is True
    assert not paths.socket_path().exists()
    assert not paths.pid_path().exists()
    assert client.daemon_running() is False
    final = client.read_state()
    assert final["active"] is False


def test_pipelined_requests_on_one_connection(running_daemon):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(str(paths.socket_path()))
        sock.sendall(b'{"id": 1, "method": "ping"}\n{"id": 2, "method": "status"}\n')
        buf = b""
        while buf.count(b"\n") < 2:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    replies = {r["id"]: r for r in (json.loads(line) for line in buf.splitlines())}
    assert replies[1]["ok"] and replies[1]["result"]["pid"] == os.getpid()
    assert replies[2]["ok"] and replies[2]["result"]["station_name"] == "Fake FM"


def test_second_daemon_refuses_to_start(running_daemon):
    second = daemon.RadioDaemon(radio_factory=FakeRadio)
    with pytest.raises(daemon.AlreadyRunning):
        asyncio.run(second.run())
    assert client.daemon_running() is True
