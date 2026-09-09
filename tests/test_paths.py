from pathlib import Path

from hermes_radio import config, paths
from hermes_radio import log as radio_log
from hermes_radio.sources import stations
from hermes_radio import visualizers


def test_radio_dir_honors_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert paths.hermes_home() == tmp_path
    assert paths.radio_dir() == tmp_path / "radio"
    assert (tmp_path / "radio").is_dir()
    assert paths.socket_path() == tmp_path / "radio" / "control.sock"
    assert paths.pid_path() == tmp_path / "radio" / "daemon.pid"
    assert paths.state_path() == tmp_path / "radio" / "state.json"
    assert paths.launcher_path() == tmp_path / "radio" / "daemon.json"
    assert paths.config_path() == tmp_path / "radio" / "config.yaml"


def test_radio_dir_defaults_to_home_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert paths.hermes_home() == Path.home() / ".hermes"


def test_dependent_paths_follow_hermes_home_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    first = (config.config_path(), radio_log.log_path(), stations.user_path(), visualizers.user_dir())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    second = (config.config_path(), radio_log.log_path(), stations.user_path(), visualizers.user_dir())
    assert all(str(p).startswith(str(tmp_path / "a" / "radio")) for p in first)
    assert all(str(p).startswith(str(tmp_path / "b" / "radio")) for p in second)


def test_config_round_trip_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config.set_volume(42)
    assert config.get_volume() == 42
    assert (tmp_path / "radio" / "config.yaml").exists()


def test_daemon_script_points_into_package():
    script = paths.daemon_script()
    assert script.name == "daemon.py"
    assert script.exists()
    assert script.parent == paths.PACKAGE_DIR
