"""RadioCLI wiring: layout position, Ctrl+R binding, mode filters, and mini-player rendering."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Window


def _hermes_root_first() -> None:
    """The hermes-agent tree must come first on sys.path so ``import cli`` resolves to Hermes."""
    import hermes_constants

    root = str(Path(hermes_constants.__file__).resolve().parent)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)


_hermes_root_first()

CLEAN_CONFIG = {
    "model": {"default": "anthropic/claude-opus-4.6", "base_url": "https://openrouter.ai/api/v1", "provider": "auto"},
    "display": {"compact": False, "tool_progress": "all"},
    "agent": {},
    "terminal": {"env_type": "local"},
}

PROMPT_TOOLKIT_STUBS = {name: MagicMock() for name in (
    "prompt_toolkit", "prompt_toolkit.history", "prompt_toolkit.styles", "prompt_toolkit.patch_stdout",
    "prompt_toolkit.application", "prompt_toolkit.layout", "prompt_toolkit.layout.processors",
    "prompt_toolkit.filters", "prompt_toolkit.layout.dimension", "prompt_toolkit.layout.menus",
    "prompt_toolkit.widgets", "prompt_toolkit.key_binding", "prompt_toolkit.completion",
    "prompt_toolkit.formatted_text", "prompt_toolkit.auto_suggest",
)}

LAYOUT_KW = dict(
    sudo_widget="sudo", secret_widget="secret", approval_widget="approval", clarify_widget="clarify",
    spinner_widget="spinner", spacer="spacer", status_bar="status", input_rule_top="top-rule",
    image_bar="image-bar", input_area="input-area", input_rule_bot="bottom-rule",
    voice_status_bar="voice-status", completions_menu="completions-menu",
)

ACTIVE_STATE = {
    "version": 1, "pid": 1, "updated_at": 0.0, "active": True, "paused": False, "muted": False,
    "source_mode": "stream", "station_name": "NTS Radio 1", "title": "Live from London", "artist": "",
    "decade": 0, "country": "", "mood": "", "position": None, "duration": None, "volume": 80,
    "recording": False, "recording_path": None, "levels": [0.1, 0.5, 0.9, 0.3], "meter_active": True,
}


def _ensure_client_module():
    """The client module is being written concurrently; stand in a stub with the documented surface when it is absent."""
    try:
        import hermes_radio.client  # noqa: F401
        return False
    except ImportError:
        pass
    import hermes_radio

    stub = types.ModuleType("hermes_radio.client")

    class RadioError(Exception):
        pass

    class RadioUnavailable(Exception):
        pass

    stub.RadioError = RadioError
    stub.RadioUnavailable = RadioUnavailable
    stub.read_state = lambda: {"active": False}
    stub.call = lambda method, *, timeout=30.0, start=True, **params: "stub"
    stub.daemon_running = lambda: False
    stub.ensure_daemon = lambda timeout=8.0: None
    sys.modules["hermes_radio.client"] = stub
    hermes_radio.client = stub
    return True


CLIENT_STUBBED = _ensure_client_module()


@pytest.fixture(scope="module")
def radio_cli_module():
    """Reload upstream ``cli`` under prompt_toolkit stubs (the upstream test trick), then import RadioCLI for real."""
    with patch.dict(sys.modules, PROMPT_TOOLKIT_STUBS), patch.dict("os.environ", {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}):
        import cli as hermes_cli
        hermes_cli = importlib.reload(hermes_cli)
    import hermes_radio.cli as radio_cli
    radio_cli = importlib.reload(radio_cli)
    return hermes_cli, radio_cli


@pytest.fixture
def make_cli(radio_cli_module):
    hermes_cli, radio_cli = radio_cli_module

    def _make(**kwargs):
        with patch.object(hermes_cli, "get_tool_definitions", return_value=[]), \
                patch.dict(hermes_cli.__dict__, {"CLI_CONFIG": CLEAN_CONFIG}):
            return radio_cli.RadioCLI(**kwargs)

    return _make


def _binding_for(kb, key):
    return [b for b in kb.bindings if b.keys == (key,)]


class TestLayout:
    def test_mini_player_lands_right_after_input_rule_bot(self, make_cli):
        cli = make_cli()
        children = cli._build_tui_layout_children(**LAYOUT_KW)
        idx = children.index("bottom-rule")
        assert children[idx + 1] is cli._radio_player_widget
        assert isinstance(cli._radio_player_widget, Window)
        assert children[idx + 2] == "voice-status"

    def test_menu_overlay_sits_above_status_bar(self, make_cli):
        cli = make_cli()
        children = cli._build_tui_layout_children(**LAYOUT_KW)
        spacer_idx = children.index("spacer")
        status_idx = children.index("status")
        assert status_idx == spacer_idx + 2
        assert children[spacer_idx + 1] is not None
        assert children[spacer_idx + 1] not in LAYOUT_KW.values()

    def test_player_height_tracks_state(self, make_cli, monkeypatch):
        import hermes_radio.client as client
        cli = make_cli()
        monkeypatch.setattr(client, "read_state", lambda: {"active": False})
        cli._radio_state(refresh=True)
        assert cli._radio_player_height() == 0
        monkeypatch.setattr(client, "read_state", lambda: dict(ACTIVE_STATE))
        monkeypatch.setattr(cli, "_radio_start_ticker", lambda: None)
        cli._radio_state(refresh=True)
        assert cli._radio_player_height() == 2
        cli._radio_expanded = True
        assert cli._radio_player_height() > 2


class TestKeybindings:
    def test_ctrl_r_is_bound(self, make_cli):
        cli = make_cli()
        kb = KeyBindings()
        cli._register_extra_tui_keybindings(kb, input_area=None)
        assert _binding_for(kb, "c-r"), "c-r not bound"

    def test_control_bindings_filtered_off_when_inactive(self, make_cli, monkeypatch):
        import hermes_radio.client as client
        cli = make_cli()
        monkeypatch.setattr(client, "read_state", lambda: {"active": False})
        kb = KeyBindings()
        cli._register_extra_tui_keybindings(kb, input_area=None)
        control_keys = {"n", "m", "-", "+", "r", "<", ">", " ", "tab", "enter"}
        filtered = [b for b in kb.bindings if b.keys[0] in control_keys or b.keys == ("escape",)]
        assert filtered
        assert not any(b.filter() for b in filtered)

    def test_control_bindings_on_only_in_control_mode(self, make_cli, monkeypatch):
        import hermes_radio.client as client
        cli = make_cli()
        monkeypatch.setattr(client, "read_state", lambda: dict(ACTIVE_STATE))
        monkeypatch.setattr(cli, "_radio_start_ticker", lambda: None)
        kb = KeyBindings()
        cli._register_extra_tui_keybindings(kb, input_area=None)
        skip = _binding_for(kb, "n")[0]
        assert not skip.filter()
        cli._radio_control_mode = True
        assert skip.filter()
        cli._clarify_state = {"question": "x"}
        assert not skip.filter(), "control keys must yield to a core modal"

    def test_menu_bindings_follow_menu_state(self, make_cli):
        cli = make_cli()
        kb = KeyBindings()
        cli._register_extra_tui_keybindings(kb, input_area=None)
        down = [b for b in _binding_for(kb, "j")][0]
        assert not down.filter()
        cli._radio_open_menu()
        assert cli._radio_menu_state is not None
        assert down.filter()
        cli._radio_menu_state.cancel()
        cli._radio_menu_state = None
        assert not down.filter()


class TestMiniPlayer:
    def test_fragments_render_title_and_bars(self, make_cli, monkeypatch):
        import hermes_radio.client as client
        cli = make_cli()
        monkeypatch.setattr(client, "read_state", lambda: dict(ACTIVE_STATE))
        monkeypatch.setattr(cli, "_radio_start_ticker", lambda: None)
        cli._radio_state(refresh=True)
        fragments = cli._radio_player_fragments()
        styles = {style for style, _ in fragments}
        text = "".join(t for _, t in fragments)
        assert "class:radio-title" in styles
        assert "NTS Radio 1" in text
        bars = [t for style, t in fragments if style == "class:radio-bars"][0]
        assert any("⠀" <= ch <= "⣿" for ch in bars)
        assert "LIVE" in text
        assert "Ctrl+R" in text

    def test_expanded_fragments_match_declared_height(self):
        from hermes_radio.ui import mini_player
        for control_mode in (False, True):
            frags = mini_player.expanded_fragments(dict(ACTIVE_STATE), control_mode=control_mode)
            lines = "".join(t for _, t in frags).count("\n")
            assert lines == mini_player.expanded_height(dict(ACTIVE_STATE), control_mode=control_mode)

    def test_inactive_state_renders_nothing(self):
        from hermes_radio.ui import mini_player
        assert mini_player.mini_fragments({"active": False}, control_mode=False) == []
        assert mini_player.player_height({"active": False}, expanded=True, control_mode=False) == 0


class TestStyles:
    def test_radio_styles_merge_and_alias_upstream(self, make_cli):
        cli = make_cli()
        cli._tui_style_base = {"input-rule": "#CD7F32", "hint": "#888888 italic"}
        styles = cli._build_tui_style_dict()
        assert styles["radio-bars"] == "#bc8cff"
        assert styles["radio-border"] == "#CD7F32"
        assert styles["radio-hint"] == "#888888 italic"


class TestSlashCommand:
    def test_slash_handler_fallback_picks_up_radio(self, radio_cli_module):
        _, radio_cli = radio_cli_module
        assert radio_cli.RadioCLI._slash_handler("radio") == ("_handle_radio_command", True)

    def test_bare_radio_opens_menu_without_app(self, make_cli, monkeypatch):
        cli = make_cli()
        cli._app = object()
        cli._handle_radio_command("/radio")
        assert cli._radio_menu_state is not None
