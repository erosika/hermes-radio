"""Hermes Radio daemon. One per Hermes home; owns mpv and publishes state.json.

Serves newline-delimited JSON on control.sock as described in docs/protocol.md.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

if __package__:
    from . import log as radio_log
    from . import paths
    from .client import daemon_running
    from .player import HermesRadio
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from hermes_radio import log as radio_log
    from hermes_radio import paths
    from hermes_radio.client import daemon_running
    from hermes_radio.player import HermesRadio

logger = logging.getLogger(__name__)

STATE_HZ = 6.0
IDLE_POLL = 0.5

METHODS = frozenset({
    "ping",
    "status",
    "play_stream",
    "play_station",
    "play_somafm",
    "play_crate",
    "play_local",
    "skip",
    "toggle_pause",
    "toggle_mute",
    "set_volume",
    "adjust_volume",
    "start_recording",
    "stop_recording",
    "search",
    "stations",
    "stop",
})


class AlreadyRunning(RuntimeError):
    """Another daemon answers ping on the control socket."""


class RadioDaemon:
    """Serves one radio engine over control.sock and keeps state.json current."""

    def __init__(self, radio_factory: Optional[Callable[[], Any]] = None):
        self._factory = radio_factory or HermesRadio
        self._radio: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._connections: Set[asyncio.Task] = set()
        self._dirty = False

    # Lifecycle

    def request_stop(self) -> None:
        """Ask the daemon to shut down. Safe from signal handlers and other threads."""
        loop, stopping = self._loop, self._stopping
        if loop is None or stopping is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(stopping.set)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        sock_path = paths.socket_path()
        pid_path = paths.pid_path()

        if daemon_running():
            radio_log.error(f"another radio daemon answers on {sock_path}; not starting")
            raise AlreadyRunning(str(sock_path))
        if sock_path.exists():
            sock_path.unlink()

        self._server = await asyncio.start_unix_server(self._on_connect, path=str(sock_path))
        try:
            os.chmod(sock_path, 0o600)
        except OSError:
            pass
        pid_path.write_text(f"{os.getpid()}\n")

        self._radio = self._factory()
        self._radio.set_state_callback(self._on_state_change)
        self._install_signal_handlers()
        self._write_state()
        state_task = asyncio.create_task(self._state_loop())
        radio_log.info(f"daemon started pid={os.getpid()} socket={sock_path}")

        try:
            await self._stopping.wait()
        finally:
            state_task.cancel()
            await asyncio.gather(state_task, return_exceptions=True)
            self._server.close()
            for task in list(self._connections):
                task.cancel()
            await asyncio.gather(*self._connections, return_exceptions=True)
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            try:
                await self._radio.stop()
            except Exception:
                logger.exception("radio stop failed during shutdown")
            self._write_state()
            self._remove_files(sock_path, pid_path)
            radio_log.info("daemon exited")

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                # Not on the main thread (tests) or no signal support; a stop request still works.
                pass

    @staticmethod
    def _remove_files(sock_path: Path, pid_path: Path) -> None:
        try:
            sock_path.unlink()
        except OSError:
            pass
        try:
            if pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass

    # state.json

    def _active(self) -> bool:
        try:
            return bool(self._radio.snapshot().get("active"))
        except Exception:
            return False

    def _on_state_change(self) -> None:
        self._dirty = True
        self._write_state()

    def _write_state(self) -> bool:
        """Write state.json atomically. Returns the snapshot's active flag."""
        self._dirty = False
        try:
            state = self._radio.snapshot()
            state["pid"] = os.getpid()
            state["updated_at"] = time.time()
            path = paths.state_path()
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(state, default=str))
            os.replace(tmp, path)
            return bool(state.get("active"))
        except Exception:
            logger.debug("state.json write failed", exc_info=True)
            return False

    async def _state_loop(self) -> None:
        try:
            while True:
                active = self._write_state() if (self._dirty or self._active()) else False
                await asyncio.sleep(1.0 / STATE_HZ if active else IDLE_POLL)
        except asyncio.CancelledError:
            pass

    # control.sock

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        lock = asyncio.Lock()
        inflight: Set[asyncio.Task] = set()
        try:
            while not self._stopping.is_set():
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError) as e:
                    await self._reply(writer, lock, {"id": None, "ok": False, "error": f"request line too long: {e}"})
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                request = asyncio.create_task(self._serve_line(line, writer, lock))
                inflight.add(request)
                request.add_done_callback(inflight.discard)
            # A client that hangs up mid-request still gets its command executed.
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("connection handler error")
        finally:
            self._connections.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_line(self, line: bytes, writer: asyncio.StreamWriter, lock: asyncio.Lock) -> None:
        req_id = None
        method = ""
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            req_id = request.get("id")
            method = request.get("method")
            params = request.get("params")
            if params is None:
                params = {}
            if not isinstance(method, str) or not method:
                raise ValueError("request has no method")
            if not isinstance(params, dict):
                raise ValueError("params must be a JSON object")
            result = await self._dispatch(method, params)
            reply: Dict[str, Any] = {"id": req_id, "ok": True, "result": result}
        except Exception as e:
            message = str(e) or e.__class__.__name__
            logger.debug("request failed: %s", message, exc_info=True)
            reply = {"id": req_id, "ok": False, "error": message}

        await self._reply(writer, lock, reply)
        if method == "stop" and reply["ok"]:
            self._stopping.set()

    async def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "ping":
            return {"pid": os.getpid(), "active": self._active()}
        if method not in METHODS:
            raise ValueError(f"unknown method: {method}")
        handler = getattr(self._radio, method)
        result = handler(**params)
        if inspect.isawaitable(result):
            result = await result
        return result

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, lock: asyncio.Lock, reply: Dict[str, Any]) -> None:
        try:
            data = (json.dumps(reply, default=str) + "\n").encode("utf-8")
        except (TypeError, ValueError) as e:
            data = (json.dumps({"id": reply.get("id"), "ok": False, "error": f"unserializable result: {e}"}) + "\n").encode("utf-8")
        async with lock:
            try:
                writer.write(data)
                await writer.drain()
            except (ConnectionError, RuntimeError):
                pass


def main() -> int:
    logging.basicConfig(
        filename=str(radio_log.log_path()),
        level=logging.INFO,
        format="%(asctime)s  %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(RadioDaemon().run())
    except AlreadyRunning:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
