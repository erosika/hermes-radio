"""The ``/radio`` slash grammar. Every branch returns plain text with no ANSI."""

from __future__ import annotations

import shlex
from typing import Any, Callable, Dict, List

from . import client

HELP = """\
/radio                       now playing
/radio play <name|url>       curated station, Radio Browser match, or a stream URL
/radio soma [channel]        SomaFM channel, or list the channels
/radio crate [1970 JPN slow] Radiooooo crate dig by decade, country, mood
/radio local <path>          local file or directory
/radio pause                 pause or resume
/radio skip                  next track
/radio mute                  mute or unmute
/radio vol <0-100> | +N | -N volume
/radio rec [start|stop]      record the stream to disk
/radio mic [text]            DJ mic break
/radio search <query>        search Radio Browser
/radio stations              curated station list
/radio viz [name|next|prev]  visualizer preset
/radio stop                  stop playback and the radio daemon
/radio help                  this list"""

MOODS = ("slow", "fast", "weird")


def parse_crate(tokens: List[str]) -> Dict[str, Any]:
    """Split crate tokens into decades, moods, and a country code."""
    decades: List[int] = []
    moods: List[str] = []
    country = None
    for token in tokens:
        low = token.lower()
        if token.isdigit() and len(token) == 4 and 1900 <= int(token) <= 2020:
            decades.append(int(token) // 10 * 10)
        elif low in MOODS:
            moods.append(low)
        elif len(token) == 3 and token.isalpha():
            country = token.upper()
    return {"decades": decades or None, "moods": moods or None, "country": country}


def _format_time(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def status_line(state: Dict[str, Any]) -> str:
    if not state.get("active"):
        return "Radio is off. Try /radio play nts"
    parts: List[str] = []
    station = state.get("station_name") or ""
    title = state.get("title") or ""
    artist = state.get("artist") or ""
    if artist and artist != "Unknown":
        parts.append(f"{artist} — {title}" if title else artist)
    elif title:
        parts.append(title)
    if station and station not in parts:
        parts.insert(0, station)
    line = " · ".join(parts) or "…"
    if state.get("source_mode") == "crate":
        tags = [t for t in (f"{state['decade']}s" if state.get("decade") else "", state.get("country") or "",
                            state.get("mood") or "") if t]
        if tags:
            line += f"  [{' '.join(tags)}]"
    if state.get("duration"):
        line += f"  {_format_time(state.get('position') or 0)} / {_format_time(state['duration'])}"
    elif state.get("source_mode") == "stream":
        line += "  LIVE"
    line += f"  vol {int(state.get('volume') or 0)}"
    if state.get("paused"):
        line += "  paused"
    if state.get("muted"):
        line += "  muted"
    if state.get("recording"):
        line += "  REC"
    return line


def _text(result: Any) -> str:
    return result if isinstance(result, str) else str(result)


def _play(args: List[str]) -> str:
    if not args:
        return "usage: /radio play <station name or stream URL>"
    target = " ".join(args)
    if target.startswith(("http://", "https://")):
        return _text(client.call("play_stream", url=target))
    return _text(client.call("play_station", query=target))


def _soma(args: List[str]) -> str:
    result = client.call("play_somafm", channel_id=args[0] if args else "")
    if isinstance(result, dict) and "channels" in result:
        rows = [f"{ch['id']:<14} {ch['title']}  ({ch.get('genre', '')})" for ch in result["channels"]]
        return "SomaFM channels:\n" + "\n".join(rows)
    return _text(result)


def _crate(args: List[str]) -> str:
    return _text(client.call("play_crate", **parse_crate(args)))


def _local(args: List[str]) -> str:
    if not args:
        return "usage: /radio local <file or directory>"
    return _text(client.call("play_local", path=" ".join(args)))


def _vol(args: List[str]) -> str:
    if not args:
        return "usage: /radio vol <0-100> | +N | -N"
    raw = args[0]
    try:
        if raw[0] in "+-":
            return _text(client.call("adjust_volume", delta=float(raw)))
        return _text(client.call("set_volume", level=float(raw)))
    except ValueError:
        return f"not a volume: {raw}"


def _rec(args: List[str]) -> str:
    action = args[0].lower() if args else "toggle"
    if action == "toggle":
        action = "stop" if client.read_state().get("recording") else "start"
    if action == "start":
        return _text(client.call("start_recording", path=" ".join(args[1:])))
    if action == "stop":
        return _text(client.call("stop_recording"))
    return "usage: /radio rec [start|stop]"


def _mic(args: List[str]) -> str:
    return _text(client.call("mic_break", text=" ".join(args) or None))


def _search(args: List[str]) -> str:
    if not args:
        return "usage: /radio search <query>"
    result = client.call("search", query=" ".join(args), source="radio_browser")
    rows = result.get("results", []) if isinstance(result, dict) else []
    if not rows:
        return "no stations found"
    lines = [f"{s.get('name', '?')}  {s.get('country', '')}  {s.get('bitrate', '')}kbps" for s in rows]
    return "\n".join(lines)


def _stations(_: List[str]) -> str:
    result = client.call("stations")
    rows = result.get("stations", []) if isinstance(result, dict) else []
    if not rows:
        return "no curated stations"
    return "\n".join(f"{s.get('name', '?'):<24} {s.get('location', ''):<12} {s.get('genre', '')}" for s in rows)


def _viz(args: List[str]) -> str:
    from . import config
    from .visualizers import cycle_preset, list_presets

    if not args:
        return f"visualizer: {config.get_visualizer()}  (available: {', '.join(list_presets())})"
    choice = args[0].lower()
    if choice in ("next", "prev"):
        return f"visualizer: {cycle_preset(1 if choice == 'next' else -1)}"
    if choice not in list_presets():
        return f"unknown preset: {choice}  (available: {', '.join(list_presets())})"
    config.set_visualizer(choice)
    return f"visualizer: {choice}"


def _stop(_: List[str]) -> str:
    if not client.daemon_running():
        return "Radio is already off"
    return _text(client.call("stop", start=False))


def _simple(method: str) -> Callable[[List[str]], str]:
    return lambda _args: _text(client.call(method, start=False))


DISPATCH: Dict[str, Callable[[List[str]], str]] = {
    "play": _play,
    "soma": _soma,
    "somafm": _soma,
    "crate": _crate,
    "dig": _crate,
    "local": _local,
    "pause": _simple("toggle_pause"),
    "skip": _simple("skip"),
    "next": _simple("skip"),
    "mute": _simple("toggle_mute"),
    "vol": _vol,
    "volume": _vol,
    "rec": _rec,
    "record": _rec,
    "mic": _mic,
    "search": _search,
    "stations": _stations,
    "viz": _viz,
    "stop": _stop,
    "off": _stop,
    "help": lambda _args: HELP,
}


def run(raw_args: str) -> str:
    """Execute one ``/radio`` invocation and return the text to show."""
    try:
        tokens = shlex.split(raw_args or "")
    except ValueError:
        tokens = (raw_args or "").split()
    if not tokens:
        return status_line(client.read_state())
    verb, rest = tokens[0].lower(), tokens[1:]
    handler = DISPATCH.get(verb)
    if handler is None:
        return f"unknown radio command: {verb}\n{HELP}"
    try:
        return handler(rest)
    except client.RadioUnavailable as exc:
        return str(exc)
    except client.RadioError as exc:
        return f"radio error: {exc}"
