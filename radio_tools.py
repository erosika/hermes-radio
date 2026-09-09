"""Agent tools for the ``radio`` toolset. Each handler is a thin call into the radio daemon."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from typing import Any, Dict, List, Tuple

from .hermes_radio import client
from .hermes_radio.commands import parse_crate


def _is_gateway() -> bool:
    return bool(os.getenv("HERMES_SESSION_PLATFORM"))


def check_radio_available() -> bool:
    """Gateway sessions download tracks instead of playing them, so only local sessions need mpv."""
    return _is_gateway() or shutil.which("mpv") is not None


def _ok(message: Any, **extra: Any) -> str:
    payload: Dict[str, Any] = {"success": True}
    if isinstance(message, dict):
        payload.update(message)
    else:
        payload["message"] = message
    payload.update(extra)
    return json.dumps(payload)


def _fail(error: str) -> str:
    return json.dumps({"success": False, "error": error})


def _call(method: str, **params: Any) -> str:
    try:
        return _ok(client.call(method, **params))
    except client.RadioUnavailable as exc:
        return _fail(str(exc))
    except client.RadioError as exc:
        return _fail(str(exc))


def _run_async(coro):
    """Run a coroutine to completion from sync code, even when the caller already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=_runner, name="radio-gateway-dig", daemon=True)
    thread.start()
    thread.join(timeout=60)
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def _gateway_crate_dig(query: str) -> str:
    """Gateway mode has no speaker, so dig one track, download it, and hand it back as media."""
    import tempfile

    import httpx

    from .hermes_radio.history import save_track
    from .hermes_radio.sources.radiooooo import RadioooooClient

    crate = parse_crate(query.split())
    rc = RadioooooClient()
    try:
        track = await rc.dig(decades=crate["decades"], moods=crate["moods"], country=crate["country"])
        if not track or not track.audio_url:
            return _fail("No track found")
        saved = save_track(track.audio_url, track.artist, track.title, track.decade, track.country, track.mood)
        if not saved:
            tmp_dir = os.path.join(tempfile.gettempdir(), "hermes-radio")
            os.makedirs(tmp_dir, exist_ok=True)
            saved = os.path.join(tmp_dir, f"{track.id}.mp3")
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
                resp = await http.get(track.audio_url)
                resp.raise_for_status()
                with open(saved, "wb") as fh:
                    fh.write(resp.content)
        caption = f"{track.artist} — {track.title} ({track.decade}s, {track.country}, {track.mood})"
        return _ok(caption, media_tag=f"MEDIA:{saved}", track={
            "artist": track.artist, "title": track.title, "decade": track.decade,
            "country": track.country, "mood": track.mood})
    finally:
        await rc.close()


def radio_play(args: Dict[str, Any], **_: Any) -> str:
    source = str(args.get("source", "crate")).lower()
    query = str(args.get("query", "") or "").strip()
    if source == "crate":
        if _is_gateway():
            return _run_async(_gateway_crate_dig(query))
        return _call("play_crate", **parse_crate(query.split()))
    if source == "stream":
        if not query:
            return _fail("Provide a stream URL or station name")
        if query.startswith(("http://", "https://")):
            return _call("play_stream", url=query)
        return _call("play_station", query=query)
    if source == "somafm":
        return _call("play_somafm", channel_id=query)
    if source == "local":
        if not query:
            return _fail("Provide a file or directory path")
        return _call("play_local", path=query)
    return _fail(f"Unknown source: {source}")


def radio_pause(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _fail("Radio is not playing")
    return _call("toggle_pause")


def radio_stop(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _ok("Radio is not playing")
    return _call("stop")


def radio_skip(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _fail("Radio is not playing")
    return _call("skip")


def radio_status(args: Dict[str, Any], **_: Any) -> str:
    state = client.read_state()
    state.pop("levels", None)
    return json.dumps(state)


def radio_volume(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _fail("Radio is not playing")
    try:
        level = float(args.get("level", 80))
    except (TypeError, ValueError):
        return _fail("level must be a number from 0 to 100")
    return _call("set_volume", level=max(0.0, min(100.0, level)))


def radio_record(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _fail("Radio is not playing")
    action = str(args.get("action", "toggle")).lower()
    if action == "toggle":
        action = "stop" if client.read_state().get("recording") else "start"
    if action == "stop":
        return _call("stop_recording")
    return _call("start_recording", path=str(args.get("path", "") or ""))


def radio_mic_break(args: Dict[str, Any], **_: Any) -> str:
    if not client.daemon_running():
        return _fail("Radio is not playing")
    return _call("mic_break", text=args.get("text"))


def radio_search(args: Dict[str, Any], **_: Any) -> str:
    query = str(args.get("query", "") or "").strip()
    if not query:
        return _fail("Provide a search query")
    source = str(args.get("source", "radio_browser")).lower()
    return _call("search", query=query, source=source)


SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "radio_play",
        "description": (
            "Start the Hermes Radio player through the local mpv daemon. Sources: 'crate' (Radiooooo global "
            "archive, random tracks by decade/country/mood), 'stream' (live internet radio by station name or "
            "URL), 'somafm' (curated channels like dronezone, defcon, groovesalad), 'local' (local audio files). "
            "For crate, query may hold decades ('1970'), moods (slow/fast/weird), and ISO country codes ('JPN'). "
            "Use when the user asks to play music, listen to radio, or crate dig."),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["crate", "stream", "somafm", "local"],
                           "description": "Music source"},
                "query": {"type": "string",
                          "description": "Station name, stream URL, SomaFM channel id, file path, or crate params"},
            },
            "required": [],
        },
    },
    {
        "name": "radio_pause",
        "description": "Pause or resume Hermes Radio. Use for 'pause', 'resume', 'unpause'.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "radio_stop",
        "description": "Stop Hermes Radio completely and shut down the player daemon.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "radio_skip",
        "description": "Skip to the next track on Hermes Radio.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "radio_status",
        "description": "Current Hermes Radio state: station, track, volume, position, recording. Call this first "
                       "when you need to know whether the radio is on.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "radio_volume",
        "description": "Set Hermes Radio volume from 0 to 100.",
        "parameters": {
            "type": "object",
            "properties": {"level": {"type": "number", "description": "Volume 0-100"}},
            "required": ["level"],
        },
    },
    {
        "name": "radio_mic_break",
        "description": "Trigger a DJ mic break. With text the DJ says that text. Without text the daemon writes "
                       "commentary about the current and upcoming track.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Optional mic break text"}},
        },
    },
    {
        "name": "radio_record",
        "description": "Record the current stream to disk under the radio directory. action: start, stop, or "
                       "toggle.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "toggle"]},
                "path": {"type": "string", "description": "Optional output path"},
            },
        },
    },
    {
        "name": "radio_search",
        "description": "Search radio stations. Returns names and stream URLs.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Station name, genre, tag, or city"},
                "source": {"type": "string", "enum": ["radio_browser", "somafm", "radio_garden"],
                           "description": "Directory to search. radio_garden searches by city."},
            },
            "required": ["query"],
        },
    },
]

HANDLERS = {
    "radio_play": radio_play,
    "radio_pause": radio_pause,
    "radio_stop": radio_stop,
    "radio_skip": radio_skip,
    "radio_status": radio_status,
    "radio_volume": radio_volume,
    "radio_mic_break": radio_mic_break,
    "radio_record": radio_record,
    "radio_search": radio_search,
}

TOOLS: List[Tuple[str, Dict[str, Any], Any]] = [(s["name"], s, HANDLERS[s["name"]]) for s in SCHEMAS]
