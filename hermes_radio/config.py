"""Radio config persistence in ``$HERMES_HOME/radio/config.yaml``."""

from pathlib import Path
from typing import Any, Dict, List, Set

import yaml

from . import paths


def config_path() -> Path:
    return paths.config_path()


def load() -> Dict[str, Any]:
    """Load radio config. Returns empty dict if not found."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(config: Dict[str, Any]) -> None:
    """Save radio config to disk."""
    with open(config_path(), "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def get_decades() -> Set[int]:
    cfg = load()
    decades = cfg.get("decades")
    if decades and isinstance(decades, list):
        return set(decades)
    return {1950, 1960, 1970, 1980, 1990}


def set_decades(decades: Set[int]) -> None:
    cfg = load()
    cfg["decades"] = sorted(decades)
    save(cfg)


def get_moods() -> Set[str]:
    cfg = load()
    moods = cfg.get("moods")
    if moods and isinstance(moods, list):
        return set(moods)
    return {"slow", "fast", "weird"}


def set_moods(moods: Set[str]) -> None:
    cfg = load()
    cfg["moods"] = sorted(moods)
    save(cfg)


def get_volume() -> int:
    return load().get("volume", 80)


def set_volume(vol: int) -> None:
    cfg = load()
    cfg["volume"] = vol
    save(cfg)


def get_visualizer() -> str:
    return load().get("visualizer", "wide")


def set_visualizer(name: str) -> None:
    cfg = load()
    cfg["visualizer"] = name
    save(cfg)


def get_presets() -> Dict[str, Dict[str, Any]]:
    return load().get("presets", {})


def save_preset(name: str, preset: Dict[str, Any]) -> None:
    cfg = load()
    presets = cfg.setdefault("presets", {})
    presets[name] = preset
    save(cfg)


def delete_preset(name: str) -> bool:
    cfg = load()
    presets = cfg.get("presets", {})
    if name in presets:
        del presets[name]
        save(cfg)
        return True
    return False


def get_country_weights() -> Dict[str, float]:
    return load().get("country_weights", {})


def set_country_weights(weights: Dict[str, float]) -> None:
    cfg = load()
    cfg["country_weights"] = weights
    save(cfg)


def get_mood_weights() -> Dict[str, float]:
    return load().get("mood_weights", {})


def get_decade_weights() -> Dict[int, float]:
    return load().get("decade_weights", {})


def get_recent_stations() -> List[Dict[str, Any]]:
    """Recently listened stations, most recent first, max 10."""
    return load().get("recent_stations", [])


def add_recent_station(name: str, url: str, source: str = "stream") -> None:
    """Add a station to recently listened. Deduplicates by URL."""
    cfg = load()
    recent = [s for s in cfg.get("recent_stations", []) if s.get("url") != url]
    recent.insert(0, {"name": name, "url": url, "source": source})
    cfg["recent_stations"] = recent[:10]
    save(cfg)
