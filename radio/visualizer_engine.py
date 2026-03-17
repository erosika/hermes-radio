"""Terminal-native visualizer engine for Hermes Radio.

Adapts ascii-video-inspired composition ideas to prompt_toolkit-safe terminal
rendering:

    feature snapshot -> scene composition -> tone map -> character render

The engine stays cheap enough for continuous UI updates while supporting more
expressive, layered presets for the expanded radio display.
"""

from dataclasses import dataclass, field as dataclass_field
import hashlib
import math
import time
from typing import Dict, List, Tuple

from radio.level_meter import VisualizerFeatures, get_feature_snapshot
from radio.visualizers import load_preset

_BRAILLE_DENSITY = " ⠁⠃⠇⠏⠟⠿⣿"
_BLOCKS = " ▁▂▃▄▅▆▇█"
_DOTS = " .·•◉●"
_ASCII = " .,:;irsXA253hMHGS#9B&@"
_HYBRID = " .·:⠒⠶▃▅▇█"


@dataclass
class TerminalGrid:
    cols: int
    rows: int


@dataclass
class VisualizerState:
    levels: List[float] = dataclass_field(default_factory=list)
    previous_levels: List[float] = dataclass_field(default_factory=list)
    field: List[List[float]] | None = None
    seed_key: str = ""
    last_render: float = 0.0
    phase: float = 0.0
    pulses: List[dict] = dataclass_field(default_factory=list)
    last_energy: float = 0.0
    last_centroid: float = 0.5


_STATE: Dict[Tuple[str, int, int, str], VisualizerState] = {}


def _noise(seed: str, idx: int) -> float:
    digest = hashlib.md5(f"{seed}:{idx}".encode()).digest()
    return ((digest[0] << 8) | digest[1]) / 65535.0


def _noise2(seed: str, x: int, y: int) -> float:
    return _noise(f"{seed}:{x}:{y}", x * 131 + y * 17)


def _empty_field(rows: int, cols: int, fill: float = 0.0) -> List[List[float]]:
    return [[fill for _ in range(cols)] for _ in range(rows)]


def _scale_field(field: List[List[float]], factor: float) -> List[List[float]]:
    return [[cell * factor for cell in row] for row in field]


def _blur_field(field: List[List[float]], passes: int = 1) -> List[List[float]]:
    if not field:
        return field
    rows = len(field)
    cols = len(field[0])
    out = [row[:] for row in field]
    for _ in range(max(0, passes)):
        nxt = _empty_field(rows, cols)
        for y in range(rows):
            for x in range(cols):
                total = 0.0
                weight = 0.0
                for oy in (-1, 0, 1):
                    for ox in (-1, 0, 1):
                        yy = y + oy
                        xx = x + ox
                        if 0 <= yy < rows and 0 <= xx < cols:
                            w = 2.0 if ox == 0 and oy == 0 else 1.0
                            total += out[yy][xx] * w
                            weight += w
                nxt[y][x] = total / weight if weight else out[y][x]
        out = nxt
    return out


def _blend_values(base: float, top: float, mode: str) -> float:
    if mode == "add":
        return min(1.0, base + top)
    if mode == "max":
        return max(base, top)
    if mode == "difference":
        return abs(base - top)
    if mode == "multiply":
        return base * top
    # screen / default
    return 1.0 - (1.0 - base) * (1.0 - top)


def _blend_field(base: List[List[float]], top: List[List[float]], *, mode: str = "screen", opacity: float = 1.0) -> List[List[float]]:
    if not base:
        return [row[:] for row in top]
    rows = min(len(base), len(top))
    cols = min(len(base[0]), len(top[0]))
    out = _empty_field(rows, cols)
    alpha = max(0.0, min(1.0, opacity))
    for y in range(rows):
        for x in range(cols):
            blended = _blend_values(base[y][x], top[y][x], mode)
            out[y][x] = base[y][x] * (1.0 - alpha) + blended * alpha
    return out


def _clamp_field(field: List[List[float]]) -> List[List[float]]:
    return [[max(0.0, min(1.0, cell)) for cell in row] for row in field]


def _tonemap_field(field: List[List[float]], *, gamma: float = 0.85, floor: float = 0.02, contrast: float = 1.0) -> List[List[float]]:
    flat = [cell for row in field for cell in row]
    if not flat:
        return field
    lo = min(flat)
    hi = max(flat)
    if hi - lo < 1e-6:
        hi = lo + 1e-6
    out = []
    for row in field:
        new_row = []
        for cell in row:
            x = max(0.0, min(1.0, (cell - lo) / (hi - lo)))
            if x > 0.0:
                x = max(floor, x)
            x = max(0.0, min(1.0, ((x - 0.5) * contrast) + 0.5))
            new_row.append(x ** max(0.2, gamma))
        out.append(new_row)
    return out


def _synthetic_snapshot(width: int, position: float, title_seed: str) -> VisualizerFeatures:
    values: List[float] = []
    for i in range(width):
        base = 0.45 + 0.18 * math.sin(position * 1.7 + i * 0.33)
        ripple = 0.14 * math.sin(position * (0.7 + i * 0.03) + i * 0.17)
        sparkle = (_noise(title_seed, i) - 0.5) * 0.22
        values.append(max(0.0, min(1.0, base + ripple + sparkle)))
    diffs = [abs(b - a) for a, b in zip(values, values[1:])]
    energy = sum(values) / len(values) if values else 0.0
    peak = max(values) if values else 0.0
    transient = max(0.0, peak - energy)
    motion = sum(diffs) / len(diffs) if diffs else 0.0
    decay = max(0.0, energy - values[-1]) if values else 0.0
    return VisualizerFeatures(
        levels=values,
        energy=energy,
        peak=peak,
        transient=transient,
        motion=motion,
        decay=decay,
        active=False,
    )


def _resolve_features(width: int, position: float, title_seed: str) -> VisualizerFeatures:
    snapshot = get_feature_snapshot(width)
    if snapshot.active and snapshot.energy > 0.05:
        return snapshot
    return _synthetic_snapshot(width, position, title_seed)


def _resample_levels(levels: List[float], width: int) -> List[float]:
    if width <= 0:
        return []
    if not levels:
        return [0.0] * width
    if len(levels) == width:
        return list(levels)
    out: List[float] = []
    last = len(levels) - 1
    for i in range(width):
        pos = (i / max(1, width - 1)) * last
        lo = int(math.floor(pos))
        hi = min(last, lo + 1)
        mix = pos - lo
        out.append(levels[lo] * (1.0 - mix) + levels[hi] * mix)
    return out


def _smooth_levels(levels: List[float], state: VisualizerState, attack: float, decay: float, paused: bool) -> List[float]:
    if not state.levels or len(state.levels) != len(levels):
        state.levels = [0.0] * len(levels)
    now = time.time()
    dt = min(now - state.last_render, 0.5) if state.last_render else 0.25
    state.last_render = now
    if paused:
        state.levels = [value * 0.9 for value in state.levels]
        return list(state.levels)
    attack_factor = min(1.0, max(0.0, attack) * dt)
    decay_factor = min(1.0, max(0.0, decay) * dt)
    for i, value in enumerate(levels):
        current = state.levels[i]
        if value > current:
            state.levels[i] = current + (value - current) * attack_factor
        else:
            state.levels[i] = current + (value - current) * decay_factor
    return list(state.levels)


def _apply_center_boost(levels: List[float], amount: float) -> List[float]:
    if amount <= 0.0 or not levels:
        return levels
    n = len(levels)
    center = max(1.0, (n - 1) / 2)
    out: List[float] = []
    for i, value in enumerate(levels):
        dist = abs(i - center) / center
        weight = 1.0 - dist * amount
        out.append(max(0.0, min(1.0, value * weight)))
    return out


def _compute_centroid(levels: List[float]) -> float:
    if not levels:
        return 0.5
    total = sum(levels)
    if total <= 1e-6:
        return 0.5
    weighted = sum(i * v for i, v in enumerate(levels)) / total
    return weighted / max(1, len(levels) - 1)


def _add_blob(field: List[List[float]], cx: float, cy: float, radius: float, intensity: float) -> None:
    if radius <= 0.0 or intensity <= 0.0:
        return
    rows = len(field)
    cols = len(field[0]) if rows else 0
    min_x = max(0, int(cx - radius - 1))
    max_x = min(cols, int(cx + radius + 2))
    min_y = max(0, int(cy - radius - 1))
    max_y = min(rows, int(cy + radius + 2))
    for y in range(min_y, max_y):
        for x in range(min_x, max_x):
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            if dist <= radius:
                glow = (1.0 - dist / max(radius, 1e-6)) ** 1.4
                field[y][x] = max(field[y][x], intensity * glow)


def _add_line(field: List[List[float]], x0: float, y0: float, x1: float, y1: float, intensity: float, width: float = 0.6) -> None:
    steps = max(2, int(max(abs(x1 - x0), abs(y1 - y0)) * 3))
    for step in range(steps + 1):
        t = step / steps
        x = x0 + (x1 - x0) * t
        y = y0 + (y1 - y0) * t
        _add_blob(field, x, y, width, intensity)


def _add_bar(field: List[List[float]], x: int, height: float, intensity: float, *, glow: float = 0.3) -> None:
    rows = len(field)
    cols = len(field[0]) if rows else 0
    if not (0 <= x < cols):
        return
    cap = max(0.0, min(rows, height))
    for y in range(rows):
        from_bottom = rows - y
        if from_bottom <= cap:
            frac = (cap - (from_bottom - 1))
            base = intensity * max(0.15, min(1.0, frac))
            field[y][x] = max(field[y][x], base)
            if glow > 0.0:
                if x > 0:
                    field[y][x - 1] = max(field[y][x - 1], base * glow)
                if x + 1 < cols:
                    field[y][x + 1] = max(field[y][x + 1], base * glow)


def _seed_pulses(state: VisualizerState, grid: TerminalGrid, centroid: float, features: VisualizerFeatures, title_seed: str) -> None:
    if features.transient < 0.08:
        return
    x = centroid * max(1, grid.cols - 1)
    wobble = (_noise(f"pulse:{title_seed}:{len(state.pulses)}", int(features.transient * 1000)) - 0.5) * max(1.0, grid.cols * 0.08)
    state.pulses.append({
        "x": x + wobble,
        "y": (grid.rows - 1) * (0.55 - min(0.2, features.energy * 0.2)),
        "radius": 0.35,
        "intensity": min(1.0, 0.35 + features.transient * 1.25),
        "speed": 1.1 + features.motion * 4.0,
        "decay": 0.72 + (1.0 - min(0.5, features.motion)) * 0.12,
    })
    state.pulses = state.pulses[-12:]


def _apply_pulses(field: List[List[float]], state: VisualizerState, dt: float, pulse_gain: float) -> None:
    next_pulses = []
    for pulse in state.pulses:
        pulse["radius"] += pulse["speed"] * max(0.04, dt)
        pulse["intensity"] *= pulse["decay"]
        if pulse["intensity"] < 0.04:
            continue
        rows = len(field)
        cols = len(field[0]) if rows else 0
        for y in range(rows):
            for x in range(cols):
                dx = x - pulse["x"]
                dy = y - pulse["y"]
                dist = math.sqrt(dx * dx + dy * dy)
                edge = abs(dist - pulse["radius"])
                if edge <= 1.2:
                    ring = (1.0 - edge / 1.2) ** 1.5
                    field[y][x] = max(field[y][x], pulse["intensity"] * pulse_gain * ring)
        next_pulses.append(pulse)
    state.pulses = next_pulses


def _render_field(field: List[List[float]], chars: str) -> List[str]:
    ramps = {
        "braille": _BRAILLE_DENSITY,
        "blocks": _BLOCKS,
        "dots": _DOTS,
        "ascii": _ASCII,
        "hybrid": _HYBRID,
    }
    ramp = ramps.get(chars, _ASCII)
    rows = []
    for row in field:
        chars_out = []
        for cell in row:
            idx = min(len(ramp) - 1, max(0, round(cell * (len(ramp) - 1))))
            chars_out.append(ramp[idx])
        rows.append("".join(chars_out))
    return rows


def _scene_bars(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    for x, level in enumerate(levels[:grid.cols]):
        height = max(0.0, min(grid.rows, level * (grid.rows + 0.75)))
        _add_bar(field, x, height, 0.55 + level * 0.45, glow=0.18 + detail * 0.22)
    if features.peak > 0.2:
        for x, level in enumerate(levels[:grid.cols]):
            y = max(0, min(grid.rows - 1, int(round((1.0 - level) * (grid.rows - 1)))))
            field[y][x] = max(field[y][x], 0.25 + features.peak * 0.35)
    return field


def _scene_mirror(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, detail: float) -> List[List[float]]:
    mirrored = [0.0] * grid.cols
    center = (grid.cols - 1) / 2
    for x in range(grid.cols):
        src = min(len(levels) - 1, int(abs(x - center))) if levels else 0
        mirrored[x] = levels[src] if levels else 0.0

    field = _scale_field(_scene_bars(grid, mirrored, features, state, detail), 0.86)
    waveform = _empty_field(grid.rows, grid.cols)
    sweep = math.sin(state.phase * (0.65 + detail * 0.08)) * grid.rows * 0.08
    last_left = None
    for x in range(max(1, int(math.ceil(grid.cols / 2)))):
        level = mirrored[min(len(mirrored) - 1, x)] if mirrored else 0.0
        y = (1.0 - level) * max(1, grid.rows - 1)
        y += math.sin(state.phase * 0.8 + x * 0.22) * (0.18 + detail * 0.04) * grid.rows
        y += sweep
        left = (x, max(0.0, min(grid.rows - 1, y)))
        right = (grid.cols - 1 - x, left[1])
        if last_left is not None:
            _add_line(waveform, last_left[0], last_left[1], left[0], left[1], 0.18 + features.energy * 0.18, width=0.28 + detail * 0.08)
            _add_line(waveform, grid.cols - 1 - last_left[0], last_left[1], right[0], right[1], 0.18 + features.energy * 0.18, width=0.28 + detail * 0.08)
        last_left = left

    iris = _empty_field(grid.rows, grid.cols)
    eye_y = grid.rows * (0.34 + features.motion * 0.04)
    _add_blob(iris, center, eye_y, 1.2 + detail * 1.0, 0.24 + features.energy * 0.25)
    _add_blob(iris, center, eye_y, 0.55 + features.transient * 1.25, 0.34 + features.transient * 0.24)
    _add_line(iris, center, 0.0, center, grid.rows - 1, 0.08 + features.peak * 0.14, width=0.22)

    field = _blend_field(field, waveform, mode="screen", opacity=0.82)
    field = _blend_field(field, iris, mode="add", opacity=0.7)
    return field


def _scene_waveform(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    if not levels:
        return field
    last_point = None
    for x, level in enumerate(levels[:grid.cols]):
        y = (1.0 - level) * max(1, grid.rows - 1)
        point = (x, y)
        if last_point is not None:
            _add_line(field, last_point[0], last_point[1], point[0], point[1], 0.5 + features.energy * 0.45, width=0.55 + detail * 0.25)
        last_point = point
    return field


def _scene_scatter(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, title_seed: str, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    intensity = max(features.transient, features.energy * 0.7, features.motion * 0.55)
    points = max(1, int(round(intensity * grid.cols * grid.rows * (0.12 + detail * 0.18))))
    phase = int(state.phase * 10)
    for i in range(points):
        x = int(_noise(f"{title_seed}:sx:{phase}", i) * grid.cols) % max(1, grid.cols)
        y = int(_noise(f"{title_seed}:sy:{phase}", i) * grid.rows) % max(1, grid.rows)
        level = 0.25 + 0.75 * _noise(f"{title_seed}:sv:{phase}", i)
        field[y][x] = max(field[y][x], level)
        if level > 0.82 and grid.rows > 1:
            field[max(0, y - 1)][x] = max(field[max(0, y - 1)][x], level * 0.45)
    return field


def _scene_cathedral(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, title_seed: str, detail: float) -> List[List[float]]:
    center = (grid.cols - 1) / 2
    field = _scene_mirror(grid, levels, features, state, detail)
    arch = _empty_field(grid.rows, grid.cols)
    energy = 0.35 + features.energy * 0.65
    for x in range(grid.cols):
        dist = abs(x - center) / max(1.0, center)
        arch_y = (grid.rows - 1) * (0.18 + dist * dist * 0.72)
        for y in range(grid.rows):
            edge = abs(y - arch_y)
            if edge < 1.25 + detail * 0.5:
                glow = (1.0 - edge / (1.25 + detail * 0.5)) ** 1.6
                arch[y][x] = max(arch[y][x], glow * energy * (0.55 + (1.0 - dist) * 0.35))
    altar = _empty_field(grid.rows, grid.cols)
    _add_blob(altar, center, grid.rows - 1.1, 2.4 + detail * 2.0, 0.35 + features.energy * 0.45)
    _add_blob(altar, center, grid.rows * 0.55, 1.5 + features.transient * 3.0, 0.15 + features.transient * 0.55)
    field = _blend_field(field, arch, mode="screen", opacity=0.92)
    field = _blend_field(field, altar, mode="add", opacity=0.65)
    side = _empty_field(grid.rows, grid.cols)
    for idx in range(1, 4):
        off = idx * max(2, grid.cols // 10)
        x1 = max(0, int(center - off))
        x2 = min(grid.cols - 1, int(center + off))
        _add_line(side, x1, grid.rows - 1, x1, grid.rows * 0.25, 0.12 + features.energy * 0.08, width=0.35)
        _add_line(side, x2, grid.rows - 1, x2, grid.rows * 0.25, 0.12 + features.energy * 0.08, width=0.35)
    field = _blend_field(field, side, mode="screen", opacity=0.6)
    return field


def _scene_braille(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, title_seed: str, detail: float) -> List[List[float]]:
    field = _scale_field(_scene_prism(grid, levels, features, state, detail * 1.05), 0.74)
    bars = _scale_field(_scene_bars(grid, levels, features, state, detail * 0.9), 0.48)
    field = _blend_field(field, bars, mode="screen", opacity=0.7)

    center_x = grid.cols * state.last_centroid
    center_y = grid.rows * (0.32 + math.sin(state.phase * 0.7) * 0.04)
    orbital = _empty_field(grid.rows, grid.cols)
    ring_count = 2 + int(detail >= 1.1) + int(features.transient >= 0.28)
    for ring_idx in range(ring_count):
        radius_x = 1.3 + ring_idx * (1.7 + detail * 0.12) + features.energy * grid.cols * 0.03
        radius_y = 0.55 + ring_idx * (0.62 + detail * 0.04) + features.motion * grid.rows * 0.08
        points = max(18, int(24 + detail * 14 + ring_idx * 8))
        spin = state.phase * (0.24 + ring_idx * 0.07) + ring_idx * 0.85
        intensity = 0.12 + features.energy * 0.08 + ring_idx * 0.015
        blob_radius = 0.24 + detail * 0.08 + ring_idx * 0.03
        for step in range(points):
            angle = spin + (step / points) * math.tau
            x = center_x + math.cos(angle) * radius_x
            y = center_y + math.sin(angle) * radius_y
            _add_blob(orbital, x, y, blob_radius, intensity)

    crown = _empty_field(grid.rows, grid.cols)
    crown_y = max(0.0, center_y - (1.0 + detail * 0.35))
    _add_blob(crown, center_x, crown_y, 1.1 + detail * 0.7, 0.18 + features.transient * 0.22)
    if features.transient > 0.08:
        wing = max(1.2, grid.cols * (0.08 + detail * 0.02))
        _add_line(crown, center_x - wing, crown_y + 0.4, center_x, crown_y - 0.3, 0.12 + features.transient * 0.18, width=0.26)
        _add_line(crown, center_x + wing, crown_y + 0.4, center_x, crown_y - 0.3, 0.12 + features.transient * 0.18, width=0.26)

    field = _blend_field(field, orbital, mode="screen", opacity=0.86)
    field = _blend_field(field, crown, mode="add", opacity=0.72)
    return field


def _scene_plasma(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, title_seed: str, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    t = state.phase * (1.0 + detail * 0.3)
    for y in range(grid.rows):
        for x in range(grid.cols):
            xf = x / max(1, grid.cols - 1)
            yf = y / max(1, grid.rows - 1)
            band = levels[min(len(levels) - 1, x)] if levels else 0.0
            a = math.sin((xf * 7.0) + t * (1.6 + features.motion * 3.5))
            b = math.cos((yf * 6.2) - t * (1.2 + features.energy * 2.1))
            c = math.sin(((xf + yf) * 8.0) + t * 2.4 + band * 5.0)
            n = _noise2(f"plasma:{title_seed}:{int(t*10)}", x, y) - 0.5
            val = (a + b + c) / 3.0 * 0.5 + 0.5
            val = val * (0.35 + features.energy * 0.55) + n * (0.08 + detail * 0.06)
            field[y][x] = max(0.0, val)
    if features.transient > 0.08:
        _add_blob(field, grid.cols * state.last_centroid, grid.rows * 0.45, 1.8 + features.transient * 4.0, 0.35 + features.transient * 0.5)
    return field


def _scene_prism(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    if not levels:
        return field
    phase = state.phase
    offsets = (-0.18, 0.0, 0.18)
    for idx, offset in enumerate(offsets):
        last = None
        weight = 0.28 + idx * 0.08 + features.energy * 0.18
        for x, level in enumerate(levels[:grid.cols]):
            y = (1.0 - level) * max(1, grid.rows - 1)
            y += math.sin(phase * (1.2 + idx * 0.35) + x * 0.18) * offset * grid.rows
            point = (x, max(0.0, min(grid.rows - 1, y)))
            if last is not None:
                _add_line(field, last[0], last[1], point[0], point[1], weight, width=0.42 + detail * 0.2)
            last = point
    center = (grid.cols - 1) / 2
    _add_line(field, center, 0, center, grid.rows - 1, 0.12 + features.peak * 0.18, width=0.35)
    return field


def _scene_storm(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, title_seed: str, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    lattice = _empty_field(grid.rows, grid.cols)
    spacing_x = max(3, int(round(grid.cols / (5 + detail * 4))))
    spacing_y = max(2, int(round(grid.rows / (2 + detail * 2))))
    for x in range(0, grid.cols, spacing_x):
        _add_line(lattice, x, 0, x, grid.rows - 1, 0.08 + features.energy * 0.1, width=0.25)
    for y in range(0, grid.rows, spacing_y):
        _add_line(lattice, 0, y, grid.cols - 1, y, 0.06 + features.motion * 0.08, width=0.2)
    field = _blend_field(field, lattice, mode="screen", opacity=0.8)
    sparks = _scene_scatter(grid, levels, features, state, title_seed, detail)
    field = _blend_field(field, sparks, mode="add", opacity=0.75)
    if features.transient > 0.12:
        x = grid.cols * state.last_centroid
        _add_line(field, x, 0, x + (features.motion - 0.5) * grid.cols * 0.4, grid.rows - 1, 0.55 + features.transient * 0.35, width=0.55)
    bars = _scene_bars(grid, levels, features, state, detail * 0.75)
    field = _blend_field(field, bars, mode="screen", opacity=0.4)
    return field


def _scene_specter(grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, detail: float) -> List[List[float]]:
    field = _empty_field(grid.rows, grid.cols)
    waveform = _scene_waveform(grid, levels, features, state, detail * 0.8)
    field = _blend_field(field, waveform, mode="screen", opacity=0.9)
    haze = _empty_field(grid.rows, grid.cols)
    for y in range(grid.rows):
        for x in range(grid.cols):
            xf = x / max(1, grid.cols - 1)
            yf = y / max(1, grid.rows - 1)
            band = levels[min(len(levels) - 1, x)] if levels else 0.0
            v = math.sin(state.phase * 0.9 + xf * 4.2 + band * 2.0) * 0.5 + 0.5
            v *= (1.0 - abs(yf - 0.45) * 1.4)
            haze[y][x] = max(0.0, v) * (0.08 + features.energy * 0.22)
    field = _blend_field(field, haze, mode="screen", opacity=0.75)
    _add_blob(field, grid.cols * state.last_centroid, grid.rows * 0.45, 1.4 + detail * 1.4, 0.14 + features.transient * 0.4)
    return field


def _compose_scene(scene: str, grid: TerminalGrid, levels: List[float], features: VisualizerFeatures, state: VisualizerState, preset: dict, title_seed: str) -> List[List[float]]:
    detail = float(preset.get("detail", 1.0))
    if scene == "mirror":
        return _scene_mirror(grid, levels, features, state, detail)
    if scene == "scatter":
        return _scene_scatter(grid, levels, features, state, title_seed, detail)
    if scene == "waveform":
        return _scene_waveform(grid, levels, features, state, detail)
    if scene == "cathedral":
        return _scene_cathedral(grid, levels, features, state, title_seed, detail)
    if scene == "braille":
        return _scene_braille(grid, levels, features, state, title_seed, detail)
    if scene == "plasma":
        return _scene_plasma(grid, levels, features, state, title_seed, detail)
    if scene == "prism":
        return _scene_prism(grid, levels, features, state, detail)
    if scene == "stormgrid":
        return _scene_storm(grid, levels, features, state, title_seed, detail)
    if scene == "specter":
        return _scene_specter(grid, levels, features, state, detail)
    return _scene_bars(grid, levels, features, state, detail)


def render_rows(*, preset_name: str | None, width: int, rows: int, paused: bool, position: float, title_seed: str) -> List[str]:
    """Render terminal visualizer rows for the active or requested preset."""
    width = max(1, width)
    rows = max(1, rows)
    preset = load_preset(preset_name)

    key = (preset.get("name", preset_name or "default"), width, rows, title_seed)
    state = _STATE.setdefault(key, VisualizerState(seed_key=title_seed))
    now = time.time()
    dt = min(now - state.last_render, 0.5) if state.last_render else 0.25

    features = _resolve_features(max(width, 16), position, title_seed)
    levels = _resample_levels(features.levels, width)
    levels = _apply_center_boost(levels, float(preset.get("center_boost", 0.0)))
    state.previous_levels = list(state.levels) if state.levels else [0.0] * len(levels)
    levels = _smooth_levels(levels, state, float(preset.get("attack", 12.0)), float(preset.get("decay", 4.0)), paused)

    state.phase += dt * (0.45 + features.energy * 1.8 + features.motion * 1.2)
    state.last_centroid = _compute_centroid(levels)
    if not paused:
        _seed_pulses(state, TerminalGrid(width, rows), state.last_centroid, features, title_seed)

    grid = TerminalGrid(cols=width, rows=rows)
    scene = str(preset.get("scene") or preset.get("mode", "bars"))
    field = _compose_scene(scene, grid, levels, features, state, preset, title_seed)

    pulse_layer = _empty_field(rows, width)
    _apply_pulses(pulse_layer, state, dt, float(preset.get("pulse_gain", 0.9)))
    field = _blend_field(field, pulse_layer, mode="screen", opacity=min(1.0, 0.35 + features.transient * 0.8))

    if scene == "mirror" or preset.get("mirror"):
        mirrored = _empty_field(rows, width)
        for y in range(rows):
            for x in range(width):
                mx = width - 1 - x
                mirrored[y][x] = max(field[y][x], field[y][mx])
        field = mirrored

    trail = float(preset.get("trail", 0.28))
    if state.field is not None:
        if paused:
            field = _blend_field(_scale_field(state.field, 0.92), field, mode="screen", opacity=0.45)
        else:
            field = _blend_field(_scale_field(state.field, trail), field, mode="screen", opacity=1.0)

    blur_passes = max(0, int(round(float(preset.get("blur", 0.0)))))
    if blur_passes:
        field = _blur_field(field, blur_passes)

    state.field = _clamp_field(field)
    mapped = _tonemap_field(
        state.field,
        gamma=float(preset.get("gamma", 0.82)),
        floor=float(preset.get("floor", 0.02)),
        contrast=float(preset.get("contrast", 1.08)),
    )
    return _render_field(mapped, str(preset.get("chars", "braille")))
