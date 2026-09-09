"""Compact and expanded now-playing renderers, driven by the daemon's ``state.json`` dict."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

Fragments = List[Tuple[str, str]]

NUM_BARS = 16
EXPANDED_WIDTH = 68
EXPANDED_INNER = EXPANDED_WIDTH - 4

_BRAILLE_ROWS = [0x40 | 0x80, 0x04 | 0x20, 0x02 | 0x10, 0x01 | 0x08]
_BRAILLE_BASE = 0x2800
_DIAL = "○◘◓◑◒●"

CONTROL_HINT_A = "Spc pause  n skip  m mute  r rec  -/+ vol"
CONTROL_HINT_B = "</> viz  Tab size  Enter menu  q / Esc / Ctrl+R exit"
IDLE_HINT = "Ctrl+R"


def _braille_bar(height: float) -> str:
    filled = max(0, min(4, round(height * 4)))
    code = 0
    for i in range(filled):
        code |= _BRAILLE_ROWS[i]
    return chr(_BRAILLE_BASE + code)


def _braille_bar_stack(height: float, rows: int = 3) -> List[str]:
    """Stack ``rows`` braille cells for one column, top row first."""
    total = max(1, rows * 4)
    remaining = max(0, min(total, round(height * total)))
    out = []
    for _ in range(rows):
        fill = min(4, remaining)
        code = 0
        for i in range(fill):
            code |= _BRAILLE_ROWS[i]
        out.append(chr(_BRAILLE_BASE + code))
        remaining = max(0, remaining - 4)
    return list(reversed(out))


def _resample(values: List[float], width: int) -> List[float]:
    width = max(1, width)
    if not values:
        return [0.0] * width
    if len(values) == 1:
        return [values[0]] * width
    last = len(values) - 1
    out = []
    for i in range(width):
        pos = i * last / max(1, width - 1)
        lo = int(pos)
        hi = min(last, lo + 1)
        mix = pos - lo
        out.append(values[lo] * (1.0 - mix) + values[hi] * mix)
    return out


def _braille_rows(levels: List[float], width: int, rows: int) -> List[str]:
    """Fallback renderer: plain braille columns straight from the level history."""
    columns = [_braille_bar_stack(max(0.0, min(1.0, v)), rows) for v in _resample(list(levels), width)]
    return ["".join(col[r] for col in columns) for r in range(rows)]


def render_bars(state: Dict[str, Any], width: int, rows: int, *, preset_name: str | None) -> List[str]:
    """Render visualizer rows from ``state["levels"]`` through the preset engine, falling back to raw braille."""
    levels = list(state.get("levels") or [])
    if not state.get("meter_active") and not levels:
        return _braille_rows([], width, rows)
    try:
        from ..level_meter import features_from_levels
        from ..visualizers.engine import render_rows

        features = features_from_levels(levels, width)
        out = render_rows(
            preset_name=preset_name,
            width=width,
            rows=rows,
            paused=bool(state.get("paused")),
            position=float(state.get("position") or 0.0),
            title_seed=f"{state.get('artist', '')}-{state.get('title', '')}" or "x",
            features=features,
        )
        if out:
            return out
    except Exception:
        pass
    return _braille_rows(levels, width, rows)


def _gradient_fragments(row: str, steps: int = 6) -> Fragments:
    if not row:
        return []
    steps = max(1, min(steps, len(row)))
    width = len(row)
    fragments: Fragments = []
    start = 0
    for idx in range(steps):
        end = round((idx + 1) * width / steps)
        chunk = row[start:end]
        if chunk:
            fragments.append((f"class:radio-bars-grad-{idx}", chunk))
        start = end
    return fragments


def _format_time(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _volume_dial(volume: Any) -> str:
    vol = max(0, min(100, int(volume or 0)))
    return f"{_DIAL[min(5, vol * 6 // 101)]} {vol}"


def _rec_fragment(now: float, *, leading: str = "  ") -> Tuple[str, str]:
    if int(now * 2) % 2 == 0:
        return ("class:radio-rec", f"{leading}● REC")
    return ("class:radio-rec-dim", f"{leading}○ REC")


def _display_title(state: Dict[str, Any]) -> str:
    artist = state.get("artist") or ""
    title = state.get("title") or ""
    station = state.get("station_name") or ""
    if artist and artist != "Unknown":
        display = f"{artist} — {title}" if title else artist
    elif title and title not in ("...", "channel.mp3"):
        display = title
    else:
        display = ""
    if state.get("source_mode") == "stream" and station:
        display = f"{station} — {display}" if display and display != station else station
    return display or station or "..."


def _tags(state: Dict[str, Any]) -> List[str]:
    tags = []
    if state.get("decade"):
        tags.append(f"{state['decade']}s")
    if state.get("country"):
        tags.append(str(state["country"]))
    if state.get("mood"):
        tags.append(str(state["mood"]))
    return tags


def _active_preset() -> Dict[str, Any]:
    try:
        from .. import config
        from ..visualizers import load_preset

        return load_preset(config.get_visualizer())
    except Exception:
        return {"name": None, "rows": 3}


def mini_fragments(state: Dict[str, Any], *, control_mode: bool, now: float | None = None) -> Fragments:
    """Two-row compact bar: bars + title + volume, then hints, progress, or LIVE."""
    if not state.get("active"):
        return []
    now = time.time() if now is None else now
    is_stream = state.get("source_mode") == "stream"
    fragments: Fragments = []

    bars = render_bars(state, NUM_BARS, 1, preset_name=None)
    fragments.append(("class:radio-bars", f"  {bars[0] if bars else ''} "))

    display = _display_title(state)
    if len(display) > 38:
        display = display[:35] + "..."
    fragments.append(("class:radio-title", display))

    if state.get("source_mode") == "crate" and (state.get("decade") or state.get("country")):
        fragments.append(("class:radio-tags", f"  [{' '.join(_tags(state))}]"))
    elif is_stream and state.get("station_name"):
        fragments.append(("class:radio-station", f"  [{state['station_name']}]"))

    fragments.append(("class:radio-vol", f"  {_volume_dial(state.get('volume'))}"))
    if state.get("muted"):
        fragments.append(("class:radio-vol", "  muted"))
    if state.get("recording"):
        fragments.append(_rec_fragment(now))

    fragments.append(("", "\n"))
    duration = state.get("duration")
    position = state.get("position")
    if control_mode:
        hint_a = CONTROL_HINT_A.replace("n skip  ", "") if is_stream else CONTROL_HINT_A
        fragments.append(("class:radio-control", f"  {hint_a}  {CONTROL_HINT_B}"))
    elif not is_stream and duration and position is not None:
        time_str = f" {_format_time(position)}/{_format_time(duration)}"
        bar_width = 52 - len(time_str)
        filled = int(max(0.0, min(1.0, position / duration)) * bar_width)
        fragments.append(("", "  "))
        fragments.append(("class:radio-progress", "━" * filled + "╸"))
        fragments.append(("class:radio-progress-bg", "─" * max(0, bar_width - filled - 1)))
        fragments.append(("class:radio-time", time_str))
        fragments.append(("class:radio-hint", f"  {IDLE_HINT}"))
    elif is_stream:
        fragments.append(("class:radio-station", "  ● LIVE"))
        fragments.append(("class:radio-hint", f"  {IDLE_HINT}"))
    else:
        fragments.append(("", "  "))
        fragments.append(("class:radio-progress-bg", "─" * 52))
        fragments.append(("class:radio-hint", f"  {IDLE_HINT}"))
    return fragments


def _cell_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _boxline(fragments: Fragments, text: str, style: str) -> None:
    """One bordered row, truncated by terminal cell width so CJK titles stay inside the box."""
    max_w = EXPANDED_INNER
    used = 0
    cut = len(text)
    for i, ch in enumerate(text):
        cw = 2 if ord(ch) > 0x2E80 else 1
        if used + cw > max_w - 1:
            cut = i
            break
        used += cw
    line = text[:cut] + ("…" if cut < len(text) else "")
    pad = max(0, max_w - _cell_width(line))
    fragments.append(("class:radio-border", "  │ "))
    fragments.append((style, line))
    fragments.append(("", " " * (pad + 1)))
    fragments.append(("class:radio-border", "│\n"))


def _expanded_text_lines(state: Dict[str, Any]) -> List[Tuple[str, str]]:
    lines: List[Tuple[str, str]] = []
    if state.get("source_mode") == "stream":
        station = state.get("station_name") or ""
        if station:
            lines.append((station, "class:radio-title"))
        title = state.get("title") or ""
        if title and title != station:
            lines.append((title, "class:radio-title-dim"))
        return lines
    artist = state.get("artist") or ""
    artist = artist if artist != "Unknown" else ""
    if artist:
        lines.append((artist, "class:radio-title"))
    title = state.get("title") or "..."
    if title != artist:
        lines.append((title, "class:radio-title-dim"))
    tags = _tags(state)
    if tags:
        lines.append((" · ".join(tags), "class:radio-tags"))
    return lines


def expanded_height(state: Dict[str, Any], *, control_mode: bool) -> int:
    """Exact line count of ``expanded_fragments`` for the same inputs."""
    if not state.get("active"):
        return 0
    preset = _active_preset()
    viz_rows = max(1, int(preset.get("rows", 3)))
    # borders, header, separator, two spacers, hint row(s), progress row
    return 8 + viz_rows + len(_expanded_text_lines(state)) + (1 if control_mode else 0)


def expanded_fragments(state: Dict[str, Any], *, control_mode: bool, now: float | None = None) -> Fragments:
    """Boxed player: header with volume, multi-row visualizer, titles, hints, progress."""
    if not state.get("active"):
        return []
    now = time.time() if now is None else now
    W = EXPANDED_WIDTH
    hline = "─" * (W - 2)
    is_stream = state.get("source_mode") == "stream"
    fragments: Fragments = []

    fragments.append(("class:radio-border", f"  ╭{hline}╮\n"))

    vol_str = _volume_dial(state.get("volume"))
    right = (" ● REC  " if state.get("recording") else "") + vol_str
    title_full = "HERMES RADIO — TRANSMISSIONS ONLY"
    pad = W - 4 - len(title_full) - len(right)
    fragments.append(("class:radio-border", "  │ "))
    fragments.append(("class:radio-label", "HERMES RADI"))
    fragments.append(("class:radio-control", "O"))
    fragments.append(("class:radio-tags", " — TRANSMISSIONS ONLY"))
    fragments.append(("", " " * max(1, pad)))
    if state.get("recording"):
        fragments.append(_rec_fragment(now, leading=" "))
        fragments.append(("", "  "))
    fragments.append(("class:radio-vol", vol_str))
    fragments.append(("class:radio-border", " │\n"))

    fragments.append(("class:radio-border", f"  ├{hline}┤\n"))
    fragments.append(("class:radio-border", "  │"))
    fragments.append(("", " " * (W - 2)))
    fragments.append(("class:radio-border", "│\n"))

    preset = _active_preset()
    rows = max(1, int(preset.get("rows", 3)))
    for row in render_bars(state, EXPANDED_INNER, rows, preset_name=preset.get("name")):
        fragments.append(("class:radio-border", "  │ "))
        fragments.extend(_gradient_fragments(row, steps=6))
        fragments.append(("", " " * max(0, W - 4 - len(row) + 1)))
        fragments.append(("class:radio-border", "│\n"))

    fragments.append(("class:radio-border", "  │"))
    fragments.append(("", " " * (W - 2)))
    fragments.append(("class:radio-border", "│\n"))

    for text, style in _expanded_text_lines(state):
        _boxline(fragments, text, style)

    if control_mode:
        hint_a = CONTROL_HINT_A.replace("n skip  ", "") if is_stream else CONTROL_HINT_A
        _boxline(fragments, hint_a, "class:radio-control")
        _boxline(fragments, CONTROL_HINT_B, "class:radio-control")
    else:
        _boxline(fragments, f"{IDLE_HINT} controls", "class:radio-hint")

    bar_w = W - 18
    duration = state.get("duration")
    position = state.get("position")
    fragments.append(("class:radio-border", "  │ "))
    if not is_stream and duration and position is not None:
        filled = int(max(0.0, min(1.0, position / duration)) * bar_w)
        time_str = f" {_format_time(position)} / {_format_time(duration)}"
        fragments.append(("class:radio-progress", "━" * filled + "╸"))
        fragments.append(("class:radio-progress-bg", "─" * max(0, bar_w - filled - 1)))
        fragments.append(("class:radio-time", time_str))
        fragments.append(("", " " * max(0, W - 4 - bar_w - len(time_str) + 1)))
    elif is_stream:
        live = "● LIVE"
        fragments.append(("class:radio-station", live))
        fragments.append(("", " " * max(0, W - 4 - len(live) + 1)))
    else:
        fragments.append(("class:radio-progress-bg", "─" * bar_w))
        fragments.append(("class:radio-time", "  ∞ "))
        fragments.append(("", " " * max(0, W - 4 - bar_w - 4 + 1)))
    fragments.append(("class:radio-border", "│\n"))

    fragments.append(("class:radio-border", f"  ╰{hline}╯\n"))
    return fragments


def player_height(state: Dict[str, Any], *, expanded: bool, control_mode: bool) -> int:
    """0 when inactive, 2 for the compact bar, the box height when expanded."""
    if not state.get("active"):
        return 0
    if not expanded:
        return 2
    return expanded_height(state, control_mode=control_mode)


def player_fragments(state: Dict[str, Any], *, expanded: bool, control_mode: bool) -> Fragments:
    if expanded:
        return expanded_fragments(state, control_mode=control_mode)
    return mini_fragments(state, control_mode=control_mode)
