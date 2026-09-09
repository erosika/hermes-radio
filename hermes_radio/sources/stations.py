"""Curated station list. The bundled stations.yaml, overridden by the user's copy in the radio dir."""

from pathlib import Path
from typing import Any, Dict, List

import yaml

from .. import paths

BUILTIN_PATH = Path(__file__).parent / "stations.yaml"


def user_path() -> Path:
    return paths.radio_dir() / "stations.yaml"


def load_stations() -> List[Dict[str, Any]]:
    """Load curated stations. User file overrides built-in."""
    user = user_path()
    path = user if user.exists() else BUILTIN_PATH
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("stations", []) if isinstance(data, dict) else []
    except Exception:
        return []
