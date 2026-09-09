"""Radio menu data model and prompt_toolkit renderer. The CLI wrapper owns the keybindings and executes selections."""

from __future__ import annotations

import random
import shutil
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import config

BOX_TL, BOX_TR, BOX_BL, BOX_BR = "╭", "╮", "╰", "╯"
BOX_H, BOX_V = "─", "│"
TOGGLE_ON, TOGGLE_OFF = "●", "○"

MENU_WIDTH = 68
INNER_WIDTH = MENU_WIDTH - 4

DEFAULT_DECADES = {1950, 1960, 1970, 1980, 1990}
DEFAULT_MOODS = {"slow", "fast", "weird"}

CRATE_COUNTRIES = [
    ("Japan", "JPN"), ("France", "FRA"), ("UK", "GBR"), ("USA", "USA"),
    ("Brazil", "BRA"), ("Senegal", "SEN"), ("Nigeria", "NGA"), ("Egypt", "EGY"),
    ("India", "IND"), ("Korea", "KOR"), ("Turkey", "TUR"), ("Greece", "GRC"),
    ("Cuba", "CUB"), ("Colombia", "COL"), ("Mexico", "MEX"), ("Thailand", "THA"),
    ("Indonesia", "IDN"), ("Iran", "IRN"), ("Argentina", "ARG"),
]


def _list_presets() -> List[str]:
    try:
        from ..visualizers import list_presets
        return list_presets()
    except Exception:
        return []


def _load_stations() -> List[Dict[str, Any]]:
    try:
        from ..sources.stations import load_stations
        return load_stations()
    except Exception:
        return []


class MenuItem:
    __slots__ = ("label", "sublabel", "action", "data", "is_header", "is_toggle", "toggled", "toggle_key")

    def __init__(self, label: str, sublabel: str = "", action: str = "", data: Optional[Dict[str, Any]] = None,
                 is_header: bool = False, is_toggle: bool = False, toggled: bool = False, toggle_key: str = ""):
        self.label = label
        self.sublabel = sublabel
        self.action = action
        self.data = data or {}
        self.is_header = is_header
        self.is_toggle = is_toggle
        self.toggled = toggled
        self.toggle_key = toggle_key


class RadioMenuState:
    """Cursor, toggles, and result for one open menu. ``done`` is set when a selection or cancel lands."""

    def __init__(self, items: List[MenuItem]):
        self.items = items
        self.selectable = [i for i, it in enumerate(items) if not it.is_header]
        self.cursor = 0
        self.result: Optional[MenuItem] = None
        self.done = threading.Event()
        self.viewport_start = 0
        try:
            self.active_decades: Set[int] = set(config.get_decades())
            self.active_moods: Set[str] = set(config.get_moods())
            self.mic_breaks: bool = bool(config.get_mic_breaks())
        except Exception:
            self.active_decades = set(DEFAULT_DECADES)
            self.active_moods = set(DEFAULT_MOODS)
            self.mic_breaks = True
        self._in_crate_config = False
        self._crate_country: Optional[str] = None
        self._active_countries: Set[str] = set()
        self._sync_toggles()

    def _sync_toggles(self) -> None:
        for item in self.items:
            if not item.is_toggle:
                continue
            tk = item.toggle_key
            if tk.startswith("decade:"):
                item.toggled = int(tk.split(":")[1]) in self.active_decades
            elif tk.startswith("mood:"):
                item.toggled = tk.split(":")[1] in self.active_moods
            elif tk == "mic_breaks":
                item.toggled = self.mic_breaks
            elif tk.startswith("country:"):
                item.toggled = tk.split(":")[1] in self._active_countries
            elif tk == "save_tracks":
                try:
                    item.toggled = bool(config.load().get("save_tracks", False))
                except Exception:
                    pass

    @property
    def cursor_abs(self) -> int:
        if not self.selectable:
            return 0
        return self.selectable[min(self.cursor, len(self.selectable) - 1)]

    def move_up(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def move_down(self) -> None:
        self.cursor = min(len(self.selectable) - 1, self.cursor + 1)

    def page_up(self, page_size: int = 10) -> None:
        self.cursor = max(0, self.cursor - page_size)

    def page_down(self, page_size: int = 10) -> None:
        self.cursor = min(len(self.selectable) - 1, self.cursor + page_size)

    def jump_to_section(self, direction: int = 1) -> None:
        pos = self.cursor + direction
        while 0 <= pos < len(self.selectable):
            abs_idx = self.selectable[pos]
            if abs_idx > 0 and self.items[abs_idx - 1].is_header:
                self.cursor = pos
                return
            pos += direction

    def toggle_current(self) -> bool:
        if not self.selectable:
            return False
        item = self.items[self.cursor_abs]
        if not item.is_toggle:
            return False
        tk = item.toggle_key
        if tk.startswith("decade:"):
            self.active_decades ^= {int(tk.split(":")[1])}
        elif tk.startswith("mood:"):
            mood = tk.split(":")[1]
            if mood in self.active_moods:
                if len(self.active_moods) > 1:
                    self.active_moods.discard(mood)
            else:
                self.active_moods.add(mood)
        elif tk == "mic_breaks":
            self.mic_breaks = not self.mic_breaks
        elif tk.startswith("country:"):
            self._active_countries ^= {tk.split(":")[1]}
        elif tk == "save_tracks":
            try:
                cfg = config.load()
                cfg["save_tracks"] = not cfg.get("save_tracks", False)
                config.save(cfg)
            except Exception:
                pass
        try:
            config.set_decades(self.active_decades)
            config.set_moods(self.active_moods)
            config.set_mic_breaks(self.mic_breaks)
        except Exception:
            pass
        self._sync_toggles()
        return True

    def select_current(self) -> None:
        """Toggle a toggle, open the crate config for the crate entry, otherwise finish with ``result`` set."""
        if not self.selectable:
            self.cancel()
            return
        item = self.items[self.cursor_abs]
        if item.is_toggle:
            self.toggle_current()
            return
        if item.action == "crate" and not self._in_crate_config:
            self._in_crate_config = True
            self._crate_country = item.data.get("country")
            self.items = build_crate_config(mic_breaks=self.mic_breaks)
            self.selectable = [i for i, it in enumerate(self.items) if not it.is_header]
            self.cursor = 0
            self.viewport_start = 0
            self._sync_toggles()
            return
        if item.action == "crate":
            item.data["decades"] = sorted(self.active_decades) or None
            item.data["moods"] = sorted(self.active_moods) or None
            if self._active_countries:
                item.data["country"] = random.choice(sorted(self._active_countries))
            elif self._crate_country:
                item.data["country"] = self._crate_country
        self.result = item
        self.done.set()

    def cancel(self) -> None:
        self.result = None
        self.done.set()


def build_crate_config(mic_breaks: bool = True) -> List[MenuItem]:
    items: List[MenuItem] = [MenuItem(label="CRATE DIGGER CONFIG", is_header=True)]
    items.append(MenuItem(label="Save MP3s to disk", sublabel="~/.hermes/radio/tracks/", is_toggle=True,
                          toggle_key="save_tracks"))
    items.append(MenuItem(label="Mic breaks", sublabel="AI DJ commentary", is_toggle=True, toggled=mic_breaks,
                          toggle_key="mic_breaks"))
    items.append(MenuItem(label="DECADES", is_header=True))
    for decade in range(1930, 2030, 10):
        items.append(MenuItem(label=f"{decade}s", is_toggle=True, toggle_key=f"decade:{decade}"))
    items.append(MenuItem(label="MOODS", is_header=True))
    for mood, desc in (("weird", "the good stuff"), ("slow", "deep, contemplative"), ("fast", "upbeat, energetic")):
        items.append(MenuItem(label=mood, sublabel=desc, is_toggle=True, toggle_key=f"mood:{mood}"))
    items.append(MenuItem(label="COUNTRIES (none = random)", is_header=True))
    for name, code in CRATE_COUNTRIES:
        items.append(MenuItem(label=name, sublabel=code, is_toggle=True, toggle_key=f"country:{code}"))
    items.append(MenuItem(label="", is_header=True))
    items.append(MenuItem(label="Start digging", sublabel="Enter", action="crate"))
    return items


def build_menu_items(now_playing: Optional[Dict[str, Any]] = None, soma_channels=None,
                     presets: Optional[Dict[str, Dict[str, Any]]] = None, mic_breaks: bool = True) -> List[MenuItem]:
    """Top-level menu: now playing, recent, visualizer, options, crate, presets, curated stations, search."""
    try:
        active_visualizer = config.get_visualizer()
    except Exception:
        active_visualizer = "wide"
    items: List[MenuItem] = []

    if now_playing and now_playing.get("active"):
        items.append(MenuItem(label="NOW PLAYING", is_header=True))
        title = now_playing.get("title") or ""
        artist = now_playing.get("artist") or ""
        display = f"{artist} — {title}" if artist and artist != "Unknown" else title
        prefix = "⏸" if now_playing.get("paused") else "▶"
        items.append(MenuItem(label=f"{prefix} {display or now_playing.get('station_name', '')}",
                              sublabel=now_playing.get("station_name", ""), action="toggle_pause"))
        if now_playing.get("source_mode") != "stream":
            items.append(MenuItem(label="Skip track", action="skip"))
        items.append(MenuItem(label="Stop radio", action="stop"))

    try:
        recent = config.get_recent_stations()
    except Exception:
        recent = []
    recent = [s for s in recent if s.get("name") and s.get("name") != "?"]
    if recent:
        items.append(MenuItem(label="RECENT", is_header=True))
        for station in recent:
            items.append(MenuItem(label=station["name"], sublabel=station.get("source", "stream"), action="stream",
                                  data={"url": station.get("url", ""), "name": station["name"]}))

    names = _list_presets()
    if names:
        items.append(MenuItem(label="VISUALIZER", is_header=True))
        for name in names:
            items.append(MenuItem(label=name, sublabel="active" if name == active_visualizer else "",
                                  action="visualizer", data={"name": name}))

    items.append(MenuItem(label="OPTIONS", is_header=True))
    items.append(MenuItem(label="Save MP3s to disk", sublabel="~/.hermes/radio/tracks/", is_toggle=True,
                          toggle_key="save_tracks"))
    items.append(MenuItem(label="Mic breaks", sublabel="AI DJ commentary", is_toggle=True, toggled=mic_breaks,
                          toggle_key="mic_breaks"))

    items.append(MenuItem(label="CRATE DIGGER", is_header=True))
    items.append(MenuItem(label="Crate Digger", sublabel="configure + start", action="crate"))

    if presets:
        items.append(MenuItem(label="PRESETS", is_header=True))
        for name, preset in presets.items():
            data = dict(preset or {})
            data.setdefault("name", name)
            items.append(MenuItem(label=name, sublabel=str(data.get("source", "")), action="preset", data=data))

    curated = _load_stations()
    regions_seen: List[str] = []
    for station in curated:
        region = str(station.get("region", "")).upper()
        if region and region not in regions_seen:
            regions_seen.append(region)
            items.append(MenuItem(label=region, is_header=True))
        items.append(MenuItem(label=station["name"], sublabel=station.get("genre", ""), action="stream",
                              data={"url": station["url"], "name": station["name"]}))

    if soma_channels and not curated:
        items.append(MenuItem(label="SOMAFM", is_header=True))
        for ch in soma_channels:
            items.append(MenuItem(label=ch.get("title", ch.get("id", "?")), sublabel=ch.get("genre", ""),
                                  action="somafm", data={"channel_id": ch.get("id", "")}))

    items.append(MenuItem(label="SEARCH", is_header=True))
    items.append(MenuItem(label="Search Radio Browser", sublabel="/radio search <query>", action="search_rb"))
    items.append(MenuItem(label="Search Radio Garden", sublabel="by city", action="search_rg"))
    return items


def _section_header(label: str) -> str:
    pad = max(2, INNER_WIDTH - len(label) - 4)
    return f"{BOX_H}{BOX_H} {label} {BOX_H * pad}"


def _pad_line(text: str, width: int = INNER_WIDTH) -> str:
    if len(text) > width:
        return text[:width - 1] + "…"
    return text + " " * (width - len(text))


def _visible_rows() -> int:
    return max(6, shutil.get_terminal_size((80, 24)).lines - 12)


def _bordered(fragments: List[Tuple[str, str]], style: str, text: str) -> None:
    fragments.append(("class:radio-menu-border", f"  {BOX_V} "))
    fragments.append((style, _pad_line(text)))
    fragments.append(("class:radio-menu-border", f" {BOX_V}\n"))


def render_menu(state: RadioMenuState) -> List[Tuple[str, str]]:
    """Styled fragments for the menu overlay, scrolled so the cursor stays visible."""
    items = state.items
    cursor_abs = state.cursor_abs
    fragments: List[Tuple[str, str]] = []

    fragments.append(("class:radio-menu-border", f"  {BOX_TL}{BOX_H * (MENU_WIDTH - 2)}{BOX_TR}\n"))
    keys_hint = "↑↓ nav  Spc toggle  ↵ select  q close"
    gap = max(2, INNER_WIDTH - len("HERMES RADIO") - len(keys_hint))
    fragments.append(("class:radio-menu-border", f"  {BOX_V} "))
    fragments.append(("class:radio-menu-title", "HERMES RADI"))
    fragments.append(("class:radio-menu-accent", "O"))
    fragments.append(("", " " * gap))
    fragments.append(("class:radio-menu-dim", keys_hint[:INNER_WIDTH - len("HERMES RADIO") - gap]))
    fragments.append(("class:radio-menu-border", f" {BOX_V}\n"))
    fragments.append(("class:radio-menu-border", f"  ├{BOX_H * (MENU_WIDTH - 2)}┤\n"))

    visible = _visible_rows()
    vp = state.viewport_start
    margin = 4
    if cursor_abs < vp + margin:
        vp = max(0, cursor_abs - margin)
    elif cursor_abs >= vp + visible - margin:
        vp = cursor_abs - visible + margin + 1
    headers_in_view = sum(1 for i in range(vp, cursor_abs + 1) if i < len(items) and items[i].is_header)
    if cursor_abs + headers_in_view >= vp + visible - 2:
        vp = cursor_abs - visible + headers_in_view + 3
    vp = max(0, min(max(0, len(items) - visible), vp))
    state.viewport_start = vp

    if vp > 0:
        _bordered(fragments, "class:radio-menu-dim", "  ▲ more above")

    rendered = 0
    for idx in range(vp, len(items)):
        if rendered >= visible:
            break
        item = items[idx]
        if item.is_header:
            _bordered(fragments, "", "")
            rendered += 1
            if item.label:
                _bordered(fragments, "class:radio-menu-header", _section_header(item.label))
                rendered += 1
            continue
        is_selected = idx == cursor_abs
        pointer = " ▸ " if is_selected else "   "
        if item.is_toggle:
            label_text = f"{pointer}{TOGGLE_ON if item.toggled else TOGGLE_OFF} {item.label}"
        else:
            label_text = f"{pointer}  {item.label}"
        if is_selected:
            style = "class:radio-menu-selected"
        elif item.is_toggle:
            style = "class:radio-menu-on" if item.toggled else "class:radio-menu-off"
        else:
            style = "class:radio-menu-item"
        fragments.append(("class:radio-menu-border", f"  {BOX_V} "))
        if item.sublabel:
            sub_text = f"  {item.sublabel}"
            fragments.append((style, label_text))
            fragments.append(("class:radio-menu-sub", sub_text))
            fragments.append(("", " " * max(0, INNER_WIDTH - len(label_text) - len(sub_text))))
        else:
            fragments.append((style, _pad_line(label_text)))
        fragments.append(("class:radio-menu-border", f" {BOX_V}\n"))
        rendered += 1

    if vp + visible < len(items):
        _bordered(fragments, "class:radio-menu-dim", "  ▼ more below")

    fragments.append(("class:radio-menu-border", f"  ├{BOX_H * (MENU_WIDTH - 2)}┤\n"))
    decades_str = ", ".join(f"{d}s" for d in sorted(state.active_decades)) or "none"
    moods_str = ", ".join(sorted(state.active_moods)) or "none"
    try:
        viz_str = config.get_visualizer()
    except Exception:
        viz_str = "braille"
    footer = f"decades: {decades_str}  moods: {moods_str}  mic: {'on' if state.mic_breaks else 'off'}  viz: {viz_str}"
    _bordered(fragments, "class:radio-menu-dim", footer)
    fragments.append(("class:radio-menu-border", f"  {BOX_BL}{BOX_H * (MENU_WIDTH - 2)}{BOX_BR}\n"))
    return fragments


def radio_menu_fallback(now_playing: Optional[Dict[str, Any]] = None,
                        presets: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[MenuItem]:
    """Numbered print+input menu for when no prompt_toolkit application is running."""
    state = RadioMenuState(build_menu_items(now_playing=now_playing, presets=presets))
    w = INNER_WIDTH
    while True:
        print(f"\n  {BOX_TL}{BOX_H * (MENU_WIDTH - 2)}{BOX_TR}")
        print(f"  {BOX_V} {'HERMES RADIO':{w}} {BOX_V}")
        print(f"  ├{BOX_H * (MENU_WIDTH - 2)}┤")
        idx_map: Dict[int, int] = {}
        for i, item in enumerate(state.items):
            if item.is_header:
                if item.label:
                    print(f"  {BOX_V} {' ' * w} {BOX_V}")
                    print(f"  {BOX_V} {_section_header(item.label):{w}} {BOX_V}")
                continue
            num = len(idx_map) + 1
            idx_map[num] = i
            check = (TOGGLE_ON if item.toggled else TOGGLE_OFF) if item.is_toggle else " "
            sub = f"  ({item.sublabel})" if item.sublabel else ""
            print(f"  {BOX_V} {_pad_line(f'{num:2d}. {check} {item.label}{sub}', w)} {BOX_V}")
        print(f"  {BOX_BL}{BOX_H * (MENU_WIDTH - 2)}{BOX_BR}")
        try:
            raw = input("  Enter number (q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw.lower() in ("q", "quit", ""):
            return None
        try:
            choice = idx_map[int(raw)]
        except (ValueError, KeyError):
            continue
        state.cursor = state.selectable.index(choice)
        state.select_current()
        if state.done.is_set():
            return state.result
