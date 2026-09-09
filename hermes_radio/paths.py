"""Filesystem locations. Everything mutable lives under ``$HERMES_HOME/radio``."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = PACKAGE_DIR.parent


def hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def radio_dir() -> Path:
    path = hermes_home() / "radio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path() -> Path:
    return radio_dir() / "control.sock"


def pid_path() -> Path:
    return radio_dir() / "daemon.pid"


def state_path() -> Path:
    return radio_dir() / "state.json"


def launcher_path() -> Path:
    return radio_dir() / "daemon.json"


def config_path() -> Path:
    return radio_dir() / "config.yaml"


def daemon_script() -> Path:
    return PACKAGE_DIR / "daemon.py"
