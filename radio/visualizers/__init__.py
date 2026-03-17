"""Configurable visualizer presets for the Hermes Radio expanded display.

Built-in presets ship in this package. User presets live at
~/.hermes/radio/visualizers/*.yaml and override built-ins with
the same name.

Each preset defines:
  - mode/scene: bars | waveform | mirror | scatter | cathedral | braille | plasma | prism | stormgrid | specter
  - chars: character set to use (braille, blocks, ascii, dots, hybrid)
  - rows: number of vertical rows (1-6)
  - width: number of columns (16-64, or "auto" for terminal width)
  - colors: list of color hex values for gradient (top to bottom)
  - attack: smoothing attack speed (1.0-20.0)
  - decay: smoothing decay speed (1.0-10.0)
  - center_boost: whether center frequencies are visually boosted (0.0-1.0)
  - mirror: whether to mirror the bars horizontally
  - peak_hold: whether to show peak indicators (seconds, 0 = off)
"""

import os
import yaml
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent
USER_DIR = Path(os.path.expanduser("~/.hermes/radio/visualizers"))

# Default preset name
DEFAULT_PRESET = "wide"


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return None


def list_presets() -> List[str]:
    """List all available preset names (built-in + user)."""
    presets = set()
    for d in [BUILTIN_DIR, USER_DIR]:
        if d.exists():
            for f in d.glob("*.yaml"):
                if f.stem != "__init__":
                    presets.add(f.stem)
    return sorted(presets)


def load_preset(name: str = None) -> Dict[str, Any]:
    """Load a visualizer preset by name.

    Search order: user dir first, then built-in.
    Returns the preset dict with all defaults filled in.
    """
    name = name or _get_active_preset() or DEFAULT_PRESET

    # Try user dir first
    user_path = USER_DIR / f"{name}.yaml"
    if user_path.exists():
        data = _load_yaml(user_path)
        if data:
            return _apply_defaults(data)

    # Try built-in
    builtin_path = BUILTIN_DIR / f"{name}.yaml"
    if builtin_path.exists():
        data = _load_yaml(builtin_path)
        if data:
            return _apply_defaults(data)

    # Fallback to hardcoded defaults
    logger.debug("Preset '%s' not found, using defaults", name)
    return _apply_defaults({})


def cycle_preset(direction: int = 1) -> str:
    """Cycle the active preset forward/backward and persist the selection."""
    names = list_presets()
    if not names:
        return DEFAULT_PRESET

    try:
        from radio.config import get_visualizer, set_visualizer
        current = get_visualizer()
    except Exception:
        current = DEFAULT_PRESET
        set_visualizer = None

    if current not in names:
        new_name = names[0]
    else:
        step = 1 if direction >= 0 else -1
        idx = names.index(current)
        new_name = names[(idx + step) % len(names)]

    if set_visualizer is not None:
        try:
            set_visualizer(new_name)
        except Exception:
            pass
    return new_name


def _get_active_preset() -> Optional[str]:
    """Read the active visualizer preset from radio config."""
    try:
        from radio.config import load
        return load().get("visualizer")
    except Exception:
        return None


def _apply_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in missing fields with defaults."""
    defaults = {
        "name": data.get("name", "default"),
        "mode": "bars",
        "chars": "braille",  # braille | blocks | ascii | dots | hybrid
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
