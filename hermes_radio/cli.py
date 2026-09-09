"""``hermes-radio``: the classic Hermes CLI with the radio mini player, control mode, and menu.

Every player interaction goes through ``hermes_radio.client.call`` and every display read
comes from ``hermes_radio.client.read_state``; this process never owns mpv.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer, Window
from prompt_toolkit.layout.controls import FormattedTextControl

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _ensure_hermes_importable() -> None:
    """Locate the hermes-agent checkout through its installed ``hermes_constants`` module when ``cli`` is not importable."""
    try:
        import cli  # noqa: F401
        return
    except ImportError:
        pass
    import hermes_constants

    sys.path.insert(0, str(Path(hermes_constants.__file__).resolve().parent))


_ensure_hermes_importable()

from cli import HermesCLI  # noqa: E402

from .ui import menu as radio_menu  # noqa: E402
from .ui import mini_player  # noqa: E402

INACTIVE_STATE: Dict[str, Any] = {"active": False}

RADIO_STYLES = {
    "radio-menu-title": "#e6edf3 bold",
    "radio-menu-accent": "#7eb8f6 bold underline",
    "radio-menu-header": "#FFBF00 bold",
    "radio-menu-border": "#21262d",
    "radio-menu-dim": "#484f58",
    "radio-menu-sub": "#6e7681 italic",
    "radio-menu-item": "#c9d1d9",
    "radio-menu-selected": "#7eb8f6 bold",
    "radio-menu-on": "#7ee6a8",
    "radio-menu-off": "#484f58",
    "radio-bars": "#bc8cff",
    "radio-bars-grad-0": "#7eb8f6",
    "radio-bars-grad-1": "#9b8cf6",
    "radio-bars-grad-2": "#bc8cff",
    "radio-bars-grad-3": "#d48cff",
    "radio-bars-grad-4": "#bc8cff",
    "radio-bars-grad-5": "#9b8cf6",
    "radio-title": "#e6edf3 bold",
    "radio-title-dim": "#c9d1d9",
    "radio-label": "#7eb8f6 bold",
    "radio-tags": "#6e7681",
    "radio-station": "#7ee6a8",
    "radio-time": "#6e7681",
    "radio-vol": "#484f58",
    "radio-border": "#21262d",
    "radio-progress": "#7eb8f6",
    "radio-progress-bg": "#21262d",
    "radio-control": "#7ee6a8",
    "radio-rec": "#f47067 bold",
    "radio-rec-dim": "#484f58",
    "radio-hint": "#555555 italic",
}

# radio class -> upstream class whose value it copies, so skins and light-mode remaps carry over
STYLE_ALIASES = {
    "radio-border": "input-rule",
    "radio-menu-border": "input-rule",
    "radio-hint": "hint",
    "radio-menu-sub": "placeholder",
    "radio-menu-header": "clarify-title",
    "radio-menu-selected": "clarify-selected",
}

CORE_MODAL_ATTRS = (
    "_clarify_state", "_approval_state", "_slash_confirm_state", "_sudo_state",
    "_secret_state", "_model_picker_state", "_command_palette_state",
)


def _read_state() -> Dict[str, Any]:
    try:
        from . import client
        state = client.read_state()
    except Exception:
        return dict(INACTIVE_STATE)
    return state if isinstance(state, dict) else dict(INACTIVE_STATE)


def _say(text: str) -> None:
    """Print through the running prompt_toolkit app when there is one; bare prints get swallowed there."""
    try:
        from cli import _cli_visible_print
        _cli_visible_print(text)
    except Exception:
        print(text)


class RadioCLI(HermesCLI):
    _RADIO_FRAME_INTERVAL = 0.16
    _RADIO_IDLE_TICKS = 12

    _radio_menu_state: Optional[radio_menu.RadioMenuState] = None
    _radio_control_mode = False
    _radio_expanded = False
    _radio_ticker_running = False
    _radio_ticker_thread: Optional[threading.Thread] = None
    _radio_state_cache: tuple = (0.0, INACTIVE_STATE)
    _radio_player_widget: Optional[Window] = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._radio_menu_state = None
        self._radio_control_mode = False
        self._radio_expanded = False
        self._radio_ticker_running = False
        self._radio_ticker_thread = None
        self._radio_state_cache = (0.0, dict(INACTIVE_STATE))

    # state

    def _radio_state(self, *, refresh: bool = False) -> Dict[str, Any]:
        """Cached ``state.json``: at most one file read per frame interval unless ``refresh``."""
        ts, state = self._radio_state_cache
        now = time.monotonic()
        if refresh or now - ts >= self._RADIO_FRAME_INTERVAL:
            state = _read_state()
            self._radio_state_cache = (now, state)
            if state.get("active"):
                self._radio_start_ticker()
        return state

    def _radio_active(self) -> bool:
        return bool(self._radio_state().get("active"))

    def _radio_no_core_modal(self) -> bool:
        return not any(getattr(self, attr, None) for attr in CORE_MODAL_ATTRS)

    def _radio_invalidate(self) -> None:
        app = getattr(self, "_app", None)
        if app is None or getattr(self, "_terminal_io_broken", False):
            return
        try:
            app.invalidate()
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.EIO:
                self._mark_terminal_io_broken("radio")
        except Exception:
            pass

    # frame ticker

    def _radio_start_ticker(self) -> None:
        if self._radio_ticker_running or getattr(self, "_terminal_io_broken", False):
            return
        self._radio_ticker_running = True
        thread = threading.Thread(target=self._radio_tick_loop, name="radio-frame-ticker", daemon=True)
        self._radio_ticker_thread = thread
        thread.start()

    def _radio_stop_ticker(self) -> None:
        self._radio_ticker_running = False
        thread = self._radio_ticker_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.3)
        self._radio_ticker_thread = None

    def _radio_tick_loop(self) -> None:
        """Repaint on a timer while the radio is active; exits after ~2s of inactivity or a dead terminal."""
        idle = 0
        try:
            while self._radio_ticker_running:
                time.sleep(self._RADIO_FRAME_INTERVAL)
                if getattr(self, "_terminal_io_broken", False):
                    break
                state = self._radio_state(refresh=True)
                if not state.get("active"):
                    idle += 1
                    if self._radio_control_mode:
                        self._radio_control_mode = False
                        self._radio_invalidate()
                    if idle >= self._RADIO_IDLE_TICKS:
                        break
                    continue
                idle = 0
                if self._radio_control_mode and not self._radio_no_core_modal():
                    self._radio_control_mode = False
                app = getattr(self, "_app", None)
                if app is None:
                    continue
                try:
                    app.invalidate()
                except OSError as exc:
                    if getattr(exc, "errno", None) == errno.EIO:
                        self._mark_terminal_io_broken("radio_ticker")
                        break
                except Exception:
                    pass
        finally:
            self._radio_ticker_running = False

    # widgets

    def _radio_player_fragments(self):
        return mini_player.player_fragments(
            self._radio_state(), expanded=self._radio_expanded, control_mode=self._radio_control_mode)

    def _radio_player_height(self) -> int:
        return mini_player.player_height(
            self._radio_state(), expanded=self._radio_expanded, control_mode=self._radio_control_mode)

    def _radio_menu_fragments(self):
        state = self._radio_menu_state
        return radio_menu.render_menu(state) if state is not None else []

    def _build_tui_layout_children(self, **kw) -> list:
        children = super()._build_tui_layout_children(**kw)
        self._radio_player_widget = Window(
            FormattedTextControl(self._radio_player_fragments),
            height=self._radio_player_height,
            wrap_lines=False)
        children.insert(children.index(kw["input_rule_bot"]) + 1, self._radio_player_widget)
        return children

    def _get_extra_tui_widgets(self) -> list:
        widgets = list(super()._get_extra_tui_widgets())
        widgets.append(ConditionalContainer(
            Window(FormattedTextControl(self._radio_menu_fragments), wrap_lines=True),
            filter=Condition(lambda: self._radio_menu_state is not None and self._radio_no_core_modal())))
        return widgets

    def _build_tui_style_dict(self) -> dict:
        """Merge the radio classes into the base dict before upstream's skin and light-mode pass runs."""
        base = dict(getattr(self, "_tui_style_base", None) or {})
        radio = dict(RADIO_STYLES)
        for radio_key, upstream_key in STYLE_ALIASES.items():
            if base.get(upstream_key):
                radio[radio_key] = base[upstream_key]
        base.update(radio)
        self._tui_style_base = base
        return super()._build_tui_style_dict()

    # keybindings

    def _register_extra_tui_keybindings(self, kb, *, input_area) -> None:
        super()._register_extra_tui_keybindings(kb, input_area=input_area)
        cli_ref = self

        menu_open = Condition(lambda: cli_ref._radio_menu_state is not None and cli_ref._radio_no_core_modal())
        control = Condition(
            lambda: cli_ref._radio_control_mode and cli_ref._radio_menu_state is None
            and cli_ref._radio_no_core_modal())
        no_core_modal = Condition(cli_ref._radio_no_core_modal)

        def _menu(method: str):
            def handler(event):
                state = cli_ref._radio_menu_state
                if state is None:
                    return
                getattr(state, method)()
                if state.done.is_set():
                    cli_ref._radio_menu_state = None
                    if state.result is not None:
                        cli_ref._radio_execute(state.result)
                event.app.invalidate()
            return handler

        for keys, method in (
            (("up", "k"), "move_up"),
            (("down", "j"), "move_down"),
            (("pageup",), "page_up"),
            (("pagedown",), "page_down"),
            (("space",), "toggle_current"),
            (("enter",), "select_current"),
            (("q",), "cancel"),
        ):
            for key in keys:
                kb.add(key, filter=menu_open)(_menu(method))
        kb.add("escape", filter=menu_open, eager=True)(_menu("cancel"))

        def _menu_section(direction: int):
            def handler(event):
                state = cli_ref._radio_menu_state
                if state is not None:
                    state.jump_to_section(direction)
                    event.app.invalidate()
            return handler

        for key in ("tab", "right", "l"):
            kb.add(key, filter=menu_open)(_menu_section(1))
        for key in ("s-tab", "left", "h"):
            kb.add(key, filter=menu_open)(_menu_section(-1))

        @kb.add("c-r", filter=no_core_modal)
        def radio_toggle(event):
            if cli_ref._radio_menu_state is not None:
                cli_ref._radio_menu_state.cancel()
                cli_ref._radio_menu_state = None
            elif cli_ref._radio_control_mode:
                cli_ref._radio_control_mode = False
            elif cli_ref._radio_state(refresh=True).get("active"):
                cli_ref._radio_control_mode = True
            else:
                cli_ref._radio_open_menu()
            event.app.invalidate()

        def _control(fn):
            def handler(event):
                fn()
                event.app.invalidate()
            return handler

        def _exit_control():
            cli_ref._radio_control_mode = False

        def _toggle_expanded():
            cli_ref._radio_expanded = not cli_ref._radio_expanded

        def _record():
            method = "stop_recording" if cli_ref._radio_state().get("recording") else "start_recording"
            cli_ref._radio_call_async(method)

        def _open_menu_from_control():
            cli_ref._radio_control_mode = False
            cli_ref._radio_open_menu()

        for key in ("q", "c-r"):
            kb.add(key, filter=control)(_control(_exit_control))
        kb.add("escape", filter=control, eager=True)(_control(_exit_control))
        kb.add("space", filter=control)(_control(lambda: cli_ref._radio_call_async("toggle_pause")))
        kb.add("n", filter=control)(_control(lambda: cli_ref._radio_call_async("skip")))
        kb.add("m", filter=control)(_control(lambda: cli_ref._radio_call_async("toggle_mute")))
        kb.add("-", filter=control)(_control(lambda: cli_ref._radio_call_async("adjust_volume", delta=-5)))
        for key in ("+", "="):
            kb.add(key, filter=control)(_control(lambda: cli_ref._radio_call_async("adjust_volume", delta=5)))
        kb.add("r", filter=control)(_control(_record))
        kb.add("<", filter=control)(_control(lambda: cli_ref._radio_cycle_visualizer(-1)))
        kb.add(">", filter=control)(_control(lambda: cli_ref._radio_cycle_visualizer(1)))
        kb.add("tab", filter=control)(_control(_toggle_expanded))
        kb.add("enter", filter=control)(_control(_open_menu_from_control))

    # actions

    def _radio_call_async(self, method: str, *, start: bool = False, quiet: bool = True, **params) -> None:
        """Run one daemon call off the UI thread; control-mode calls stay quiet because the bar shows the result."""
        def _run():
            try:
                from . import client
                result = client.call(method, start=start, **params)
            except Exception as exc:
                _say(f"  radio: {exc}")
            else:
                if not quiet:
                    _say(f"  {result if isinstance(result, str) else json.dumps(result)}")
            self._radio_state(refresh=True)
            self._radio_invalidate()

        threading.Thread(target=_run, name=f"radio-{method}", daemon=True).start()

    def _radio_cycle_visualizer(self, direction: int) -> None:
        try:
            from .visualizers import cycle_preset
            cycle_preset(direction)
        except Exception as exc:
            _say(f"  radio: {exc}")

    def _radio_open_menu(self) -> None:
        if self._radio_menu_state is not None:
            return
        from . import config

        state = self._radio_state(refresh=True)
        now = state if state.get("active") else None
        try:
            presets = config.get_presets()
        except Exception:
            presets = {}
        items = radio_menu.build_menu_items(now_playing=now, presets=presets)
        self._radio_menu_state = radio_menu.RadioMenuState(items)
        self._radio_invalidate()

    def _radio_execute(self, item: radio_menu.MenuItem) -> None:
        action, data = item.action, item.data
        if action == "crate":
            self._radio_call_async("play_crate", start=True, quiet=False, decades=data.get("decades"),
                                   moods=data.get("moods"), country=data.get("country"))
        elif action == "somafm":
            self._radio_call_async("play_somafm", start=True, quiet=False, channel_id=data.get("channel_id", ""))
        elif action == "stream":
            if data.get("url"):
                self._radio_call_async("play_stream", start=True, quiet=False, url=data["url"],
                                       station_name=data.get("name", ""))
        elif action == "preset":
            self._radio_play_preset(data)
        elif action == "visualizer":
            from . import config
            name = data.get("name", "braille")
            config.set_visualizer(name)
            _say(f"  Visualizer set to {name}")
        elif action == "toggle_pause":
            self._radio_call_async("toggle_pause")
        elif action == "skip":
            self._radio_call_async("skip")
        elif action == "stop":
            self._radio_call_async("stop", quiet=False)
        elif action in ("search_rb", "search_rg"):
            _say("  Type /radio search <query> to search stations")
        self._radio_invalidate()

    def _radio_play_preset(self, data: Dict[str, Any]) -> None:
        source = data.get("source", "")
        if source == "custom" and data.get("url"):
            self._radio_call_async("play_stream", start=True, quiet=False, url=data["url"],
                                   station_name=data.get("name", ""))
        elif source == "somafm" and data.get("channel"):
            self._radio_call_async("play_somafm", start=True, quiet=False, channel_id=data["channel"])
        elif source == "crate":
            self._radio_call_async("play_crate", start=True, quiet=False, decades=data.get("decades"),
                                   moods=data.get("moods"), country=data.get("country"))
        else:
            _say(f"  Preset {data.get('name', '?')} has no playable source")

    # slash command

    def _handle_radio_command(self, cmd: str) -> None:
        """``/radio`` opens the menu; ``/radio <args>`` runs the shared slash grammar."""
        args = cmd.strip()[len("/radio"):].strip()
        if not args:
            if getattr(self, "_app", None) is not None:
                self._radio_open_menu()
            else:
                selected = radio_menu.radio_menu_fallback(now_playing=self._radio_state(refresh=True) or None)
                if selected is not None:
                    self._radio_execute(selected)
            return
        from . import commands

        _say(commands.run(args))
        self._radio_state(refresh=True)
        self._radio_invalidate()


def _default_toolsets():
    """Same resolution as upstream ``_build_cli_from_args``: coding posture in a code workspace, else the CLI platform set."""
    import cli as hermes_cli

    try:
        from agent.coding_context import coding_selection
        selection = coding_selection(platform="cli", config=hermes_cli.CLI_CONFIG)
    except Exception:
        selection = None
    if selection is None:
        try:
            from hermes_cli.tools_config import _get_platform_tools
            selection = sorted(_get_platform_tools(hermes_cli.CLI_CONFIG, "cli"))
        except Exception:
            selection = None
    return selection


def main() -> None:
    root = str(PLUGIN_ROOT)
    # appended, not inserted: this repo's tools.py must not shadow hermes-agent's tools package
    if root not in sys.path:
        sys.path.append(root)
    os.environ["HERMES_INTERACTIVE"] = "1"
    cli = RadioCLI(toolsets=_default_toolsets())
    cli.run()


if __name__ == "__main__":
    main()
