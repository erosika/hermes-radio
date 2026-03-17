"""RadioCLI -- HermesCLI wrapper with integrated radio TUI.

Subclasses HermesCLI and uses the protected extension hooks to inject
radio widgets, keybindings, styles, and the /radio command without
modifying any upstream code.
"""

import queue
import threading
from typing import Optional

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import Window, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl

from cli import HermesCLI


class RadioCLI(HermesCLI):

    # -- process_command override: intercept /radio ----------------------------

    def process_command(self, command: str) -> bool:
        if command.strip().lower().startswith("/radio"):
            self._handle_radio_command(command.strip())
            return True
        return super().process_command(command)

    # -- Extension hook: extra styles -----------------------------------------

    def _build_tui_style_dict(self) -> dict[str, str]:
        styles = super()._build_tui_style_dict()
        styles.update({
            # Radio menu
            'radio-menu-title': '#e6edf3 bold',
            'radio-menu-accent': '#7eb8f6 bold underline',
            'radio-menu-header': '#FFBF00 bold',
            'radio-menu-border': '#21262d',
            'radio-menu-dim': '#484f58',
            'radio-menu-sub': '#6e7681 italic',
            'radio-menu-item': '#c9d1d9',
            'radio-menu-selected': '#7eb8f6 bold',
            'radio-menu-on': '#7ee6a8',
            'radio-menu-off': '#484f58',
            # Radio mini player
            'radio-bars': '#bc8cff',
            'radio-bars-grad-0': '#7eb8f6',
            'radio-bars-grad-1': '#9b8cf6',
            'radio-bars-grad-2': '#bc8cff',
            'radio-bars-grad-3': '#d48cff',
            'radio-bars-grad-4': '#bc8cff',
            'radio-bars-grad-5': '#9b8cf6',
            'radio-title': '#e6edf3 bold',
            'radio-title-dim': '#c9d1d9',
            'radio-label': '#7eb8f6 bold',
            'radio-tags': '#6e7681',
            'radio-station': '#7ee6a8',
            'radio-time': '#6e7681',
            'radio-vol': '#484f58',
            'radio-border': '#21262d',
            'radio-progress': '#7eb8f6',
            'radio-progress-bg': '#21262d',
            'radio-control': '#7ee6a8',
            'radio-rec': '#f47067 bold',
            'radio-rec-dim': '#484f58',
            'radio-hint': '#555555 italic',
        })
        return styles

    # -- Extension hook: extra widgets ----------------------------------------

    def _get_extra_tui_widgets(self) -> list:
        cli_ref = self

        # Radio menu overlay (like clarify/approval -- appears when active)
        def _get_menu_display():
            state = cli_ref._radio_menu_state
            if not state:
                return []
            from radio.menu import render_menu
            return render_menu(state)

        radio_menu_widget = ConditionalContainer(
            Window(
                FormattedTextControl(_get_menu_display),
                wrap_lines=True,
            ),
            filter=Condition(lambda: cli_ref._radio_menu_state is not None),
        )

        # Mini player (compact now-playing bar below input)
        from radio.mini_player import get_mini_player_text, get_mini_player_height

        radio_player_widget = ConditionalContainer(
            Window(
                FormattedTextControl(get_mini_player_text),
                height=get_mini_player_height,
            ),
            filter=Condition(lambda: self._radio_is_active()),
        )

        return [radio_menu_widget, radio_player_widget]

    # -- Extension hook: extra keybindings ------------------------------------

    def _register_extra_tui_keybindings(self, kb, *, input_area) -> None:
        cli_ref = self

        # --- Radio menu navigation ---
        _menu_active = Condition(lambda: cli_ref._radio_menu_state is not None)

        @kb.add('up', filter=_menu_active)
        @kb.add('k', filter=_menu_active)
        def radio_up(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.move_up()
                event.app.invalidate()

        @kb.add('down', filter=_menu_active)
        @kb.add('j', filter=_menu_active)
        def radio_down(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.move_down()
                event.app.invalidate()

        @kb.add('pageup', filter=_menu_active)
        def radio_pgup(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.page_up()
                event.app.invalidate()

        @kb.add('pagedown', filter=_menu_active)
        def radio_pgdn(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.page_down()
                event.app.invalidate()

        @kb.add('tab', filter=_menu_active)
        @kb.add('right', filter=_menu_active)
        @kb.add('l', filter=_menu_active)
        def radio_tab(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.jump_to_section(1)
                event.app.invalidate()

        @kb.add('s-tab', filter=_menu_active)
        @kb.add('left', filter=_menu_active)
        @kb.add('h', filter=_menu_active)
        def radio_stab(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.jump_to_section(-1)
                event.app.invalidate()

        @kb.add(' ', filter=_menu_active)
        def radio_space(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.toggle_current()
                event.app.invalidate()

        @kb.add('enter', filter=_menu_active)
        def radio_enter(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.select_current()
                event.app.invalidate()

        @kb.add('q', filter=_menu_active)
        @kb.add('escape', filter=_menu_active)
        def radio_quit(event):
            if cli_ref._radio_menu_state:
                cli_ref._radio_menu_state.cancel()
                event.app.invalidate()

        # --- Radio control mode (Ctrl+O modal) ---
        _radio_control = Condition(
            lambda: getattr(cli_ref, '_radio_control_mode', False)
        )
        _no_modal = Condition(
            lambda: not cli_ref._clarify_state and not cli_ref._approval_state
            and not getattr(cli_ref, '_sudo_state', None)
            and not getattr(cli_ref, '_secret_state', None)
            and cli_ref._radio_menu_state is None
        )

        def _set_control_mode(active: bool):
            cli_ref._radio_control_mode = active
            try:
                import radio.mini_player as _mp
                _mp._control_mode_active = active
            except ImportError:
                pass
            print(f"  {'Ctrl+O controls active' if active else 'Controls closed'}")

        @kb.add('c-o')
        def radio_control_toggle(event):
            if cli_ref._radio_control_mode:
                _set_control_mode(False)
                event.app.invalidate()
                return
            if not _no_modal():
                return
            try:
                from radio.player import HermesRadio
                if HermesRadio.active():
                    _set_control_mode(True)
                    event.app.invalidate()
                    return
            except ImportError:
                pass
            # Not playing -- queue /radio command
            if hasattr(cli_ref, '_pending_input'):
                cli_ref._pending_input.put("/radio")

        @kb.add('c-c', filter=_radio_control)
        @kb.add('q', filter=_radio_control)
        @kb.add('escape', filter=_radio_control)
        def radio_control_exit(event):
            _set_control_mode(False)
            event.app.invalidate()

        @kb.add(' ', filter=_radio_control)
        def rc_pause(event):
            cli_ref._run_radio_shortcut_action("pause")
            event.app.invalidate()

        @kb.add('n', filter=_radio_control)
        def rc_skip(event):
            cli_ref._run_radio_shortcut_action("skip")
            event.app.invalidate()

        @kb.add('m', filter=_radio_control)
        def rc_mute(event):
            cli_ref._run_radio_shortcut_action("mute")
            event.app.invalidate()

        @kb.add('-', filter=_radio_control)
        def rc_vol_down(event):
            cli_ref._run_radio_shortcut_action("volume_down")
            event.app.invalidate()

        @kb.add('+', filter=_radio_control)
        @kb.add('=', filter=_radio_control)
        def rc_vol_up(event):
            cli_ref._run_radio_shortcut_action("volume_up")
            event.app.invalidate()

        @kb.add('tab', filter=_radio_control)
        def rc_toggle_expanded(event):
            try:
                from radio.mini_player import toggle_expanded
                toggle_expanded()
                event.app.invalidate()
            except ImportError:
                pass

    # -- Internal helpers -----------------------------------------------------

    def _radio_is_active(self) -> bool:
        try:
            from radio.player import HermesRadio
            return HermesRadio.active()
        except ImportError:
            return False

    def _run_radio_shortcut_action(self, action: str) -> Optional[str]:
        if not self._radio_is_active():
            return None
        try:
            from radio.player import HermesRadio
            from radio_tool import _run_radio_async as _run
        except ImportError:
            return None

        radio = HermesRadio.get()
        if action == "pause":
            return _run(radio.toggle_pause())
        if action == "skip":
            return _run(radio.skip())
        if action == "volume_up":
            return _run(radio.adjust_volume(5))
        if action == "volume_down":
            return _run(radio.adjust_volume(-5))
        if action == "mute":
            return _run(radio.toggle_mute())
        return None

    def _handle_radio_command(self, cmd: str):
        import shutil as _shutil
        if not _shutil.which("mpv"):
            print("mpv not installed. Install: brew install mpv")
            return

        try:
            from radio.menu import radio_menu_fallback
            from radio.player import HermesRadio
        except ImportError as e:
            print(f"Radio module not available: {e}")
            return

        from radio_tool import _run_radio_async as _run
        from radio.menu import build_menu_items, RadioMenuState

        # Gather state
        now = None
        if HermesRadio.active():
            np = HermesRadio.get().now_playing()
            now = {"active": np.active, "title": np.title, "artist": np.artist,
                   "paused": np.paused, "station_name": np.station_name}

        soma = None
        try:
            from radio.somafm import get_featured
            soma = _run(get_featured())
            soma = [{"id": ch.id, "title": ch.title, "genre": ch.genre} for ch in soma]
        except Exception:
            pass

        presets = self.config.get("radio", {}).get("presets", {})

        # Try TUI widget, fall back to numbered menu
        selected = None
        try:
            if self._app:
                items = build_menu_items(soma_channels=soma, now_playing=now, presets=presets)
                state = RadioMenuState(items)
                self._radio_menu_state = state
                self._app.invalidate()
                state.done.wait()
                selected = state.result
                self._radio_menu_state = None
                self._app.invalidate()
            else:
                raise RuntimeError("No app")
        except Exception:
            self._radio_menu_state = None
            selected = radio_menu_fallback(now_playing=now, soma_channels=soma, presets=presets)

        if not selected:
            return

        self._execute_radio_action(_run, HermesRadio, selected)

    def _execute_radio_action(self, _run, HermesRadio, selected):
        if selected is None:
            return

        radio = HermesRadio.get()
        radio.configure(self.config)

        action = selected.action
        data = selected.data

        def _rprint(msg):
            print(f"  {msg}")

        try:
            if action == "crate":
                radio._auto_mic_breaks = data.get("mic_breaks", True)
                result = _run(radio.play_crate(
                    decades=data.get("decades"),
                    moods=data.get("moods"),
                    country=data.get("country"),
                ))
                _rprint(result)

            elif action == "somafm":
                channel_id = data.get("channel_id", "")
                if channel_id:
                    from radio.somafm import get_channel
                    ch = _run(get_channel(channel_id))
                    if ch and ch.stream_url:
                        result = _run(radio.play_stream(
                            ch.stream_url, station_name=f"SomaFM {ch.title}",
                        ))
                        _rprint(result)
                    else:
                        _rprint(f"Channel not found: {channel_id}")

            elif action == "stream":
                url = data.get("url", "")
                name = data.get("name", "")
                if url:
                    result = _run(radio.play_stream(url, station_name=name))
                    _rprint(result)

            elif action == "preset":
                source = data.get("source", "")
                if source == "custom" and data.get("url"):
                    result = _run(radio.play_stream(
                        data["url"], station_name=data.get("name", ""),
                    ))
                    _rprint(result)
                elif source == "somafm" and data.get("channel"):
                    from radio.somafm import get_channel
                    ch = _run(get_channel(data["channel"]))
                    if ch and ch.stream_url:
                        result = _run(radio.play_stream(
                            ch.stream_url, station_name=f"SomaFM {ch.title}",
                        ))
                        _rprint(result)
                elif source == "crate":
                    result = _run(radio.play_crate(
                        decades=data.get("decades"),
                        moods=data.get("moods"),
                        country=data.get("country"),
                    ))
                    _rprint(result)

            elif action == "visualizer":
                from radio.config import set_visualizer
                name = data.get("name", "braille")
                set_visualizer(name)
                _rprint(f"Visualizer set to {name}")

            elif action == "toggle_pause":
                result = _run(radio.toggle_pause())
                _rprint(result)

            elif action == "skip":
                result = _run(radio.skip())
                _rprint(result)

            elif action == "stop":
                _run(radio.stop())
                _rprint("Radio stopped")

        except Exception as e:
            _rprint(f"Radio error: {e}")

        if self._app:
            self._app.invalidate()


def main():
    """Entry point for hermes-radio."""
    import sys
    # Ensure radio module is importable from the package root
    from pathlib import Path
    pkg_root = str(Path(__file__).resolve().parent.parent)
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)

    cli = RadioCLI()
    # Initialize radio state
    cli._radio_menu_state = None
    cli._radio_control_mode = False
    cli.run()
