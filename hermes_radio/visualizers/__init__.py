"""Visualizer presets. Built-ins ship here; ``$HERMES_HOME/radio/visualizers/*.yaml`` overrides by name.

See ``_apply_defaults`` for the preset fields.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .. import paths
from ..config import get_visualizer, load as _load_config, set_visualizer

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent
DEFAULT_PRESET = "wide"


def user_dir() -> Path:
    return paths.radio_dir() / "visualizers"


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def list_presets() -> List[str]:
    """List all available preset names (built-in + user)."""
    presets = set()
    for d in (BUILTIN_DIR, user_dir()):
        if d.exists():
            for f in d.glob("*.yaml"):
                presets.add(f.stem)
    return sorted(presets)


def load_preset(name: str = None) -> Dict[str, Any]:
    """Load a preset by name, user dir first, with every default filled in."""
    name = name or _get_active_preset() or DEFAULT_PRESET

    for base in (user_dir(), BUILTIN_DIR):
        path = base / f"{name}.yaml"
        if path.exists():
            data = _load_yaml(path)
            if data:
                return _apply_defaults(data)

    logger.debug("Preset '%s' not found, using defaults", name)
    return _apply_defaults({})


def cycle_preset(direction: int = 1) -> str:
    """Cycle the active preset forward/backward and persist the selection."""
    names = list_presets()
    if not names:
        return DEFAULT_PRESET

    try:
        current = get_visualizer()
    except Exception:
        current = DEFAULT_PRESET

    if current not in names:
        new_name = names[0]
    else:
        step = 1 if direction >= 0 else -1
        idx = names.index(current)
        new_name = names[(idx + step) % len(names)]

    try:
        set_visualizer(new_name)
    except Exception:
        pass
    return new_name


def _get_active_preset() -> Optional[str]:
    try:
        return _load_config().get("visualizer")
    except Exception:
        return None


def _apply_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    defaults = {
        "name": data.get("name", "default"),
        "mode": "bars",
        "chars": "braille",
        "rows": 3,
        "width": 32,
        "colors": ["#7eb8f6", "#9b8cf6", "#bc8cff", "#d48cff", "#bc8cff", "#9b8cf6"],
        "attack": 12.0,
        "decay": 4.0,
        "center_boost": 0.25,
        "mirror": False,
        "peak_hold": 0.0,
        "scene": data.get("scene", data.get("mode", "bars")),
        "gamma": 0.82,
        "floor": 0.02,
        "contrast": 1.08,
        "trail": 0.28,
        "pulse_gain": 0.8,
        "detail": 1.0,
        "blur": 0,
    }
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
    return data
