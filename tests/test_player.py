import json
import os
import re
from pathlib import Path

from hermes_radio import paths
from hermes_radio.player import HermesRadio

PROTOCOL = Path(__file__).resolve().parent.parent / "docs" / "protocol.md"


def protocol_state_keys() -> set:
    text = PROTOCOL.read_text()
    block = re.search(r"## `state.json`\s+```json\n(.*?)\n```", text, re.S).group(1)
    return set(json.loads(block))


def test_snapshot_matches_protocol_shape():
    radio = HermesRadio()
    snap = radio.snapshot()
    assert set(snap) == protocol_state_keys()
    assert snap["version"] == 1
    assert snap["pid"] == os.getpid()
    assert isinstance(snap["updated_at"], float)
    assert snap["active"] is False
    assert snap["paused"] is False
    assert snap["muted"] is False
    assert snap["source_mode"] == ""
    assert snap["position"] is None and snap["duration"] is None
    assert isinstance(snap["volume"], int)
    assert snap["recording"] is False
    assert snap["recording_path"] is None
    assert snap["levels"] == []
    assert snap["meter_active"] is False
    json.dumps(snap)


def test_snapshot_reads_volume_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_radio import config
    config.set_volume(33)
    assert HermesRadio().snapshot()["volume"] == 33


def test_no_module_imports_the_old_radio_package():
    offenders = []
    for py in paths.PACKAGE_DIR.rglob("*.py"):
        for line in py.read_text().splitlines():
            if re.match(r"\s*(from radio(\.|\s)|import radio(\.|\s|$))", line):
                offenders.append(f"{py.relative_to(paths.PACKAGE_DIR)}: {line.strip()}")
    assert offenders == []


def test_package_imports_as_plugin_subpackage():
    import importlib
    import sys
    import types

    saved = {k: v for k, v in sys.modules.items() if k.startswith("hermes_plugins")}
    try:
        pkg = types.ModuleType("hermes_plugins")
        pkg.__path__ = []
        plugin = types.ModuleType("hermes_plugins.hermes_radio")
        plugin.__path__ = [str(paths.PLUGIN_DIR)]
        sys.modules["hermes_plugins"] = pkg
        sys.modules["hermes_plugins.hermes_radio"] = plugin
        mod = importlib.import_module("hermes_plugins.hermes_radio.hermes_radio.daemon")
        assert mod.__name__ == "hermes_plugins.hermes_radio.hermes_radio.daemon"
        assert "hermes_plugins.hermes_radio.hermes_radio.player" in sys.modules
    finally:
        for k in [k for k in sys.modules if k.startswith("hermes_plugins")]:
            del sys.modules[k]
        sys.modules.update(saved)
