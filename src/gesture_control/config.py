"""Configuration: built-in defaults merged with config/gestures.yaml."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

# src/gesture_control/config.py -> project root is two levels above the package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "gestures.yaml"
# Optional files next to gestures.yaml, merged on top of it in this order.
# Build zips never contain them, so `unzip -o` can't overwrite them.
CALIBRATION_FILE = "calibration.yaml"   # written by --calibrate
LOCAL_FILE = "local.yaml"               # your own overrides

DEFAULTS: dict[str, Any] = {
    "camera": {
        "index": 0,
        "width": 640,
        "height": 480,
        "fps": 30,
        "fourcc": "MJPG",
        "mirror": True,
        "lock_framerate": True,
    },
    "tracker": {
        "model": "models/hand_landmarker.task",
        "num_hands": 2,
        "second_hand_check_every": 5,
        "min_detection_confidence": 0.6,
        "min_presence_confidence": 0.6,
        "min_tracking_confidence": 0.5,
    },
    "pose": {
        "extend_ratio": 1.15,
        "curl_ratio": 0.95,
        "debounce_frames": 3,
    },
    "pinch": {
        "start": 0.28,
        "release": 0.42,
        "confirm_frames": 3,
        "min_finger_reach": 1.05,
        "right_min_reach": 0.0,
        "min_facing": 0.6,
    },
    "cursor": {
        "mode": "relative",
        "anchor": "knuckles",
        "region": {"x_min": 0.2, "x_max": 0.8, "y_min": 0.2, "y_max": 0.7},
        "filter": {"min_cutoff": 1.0, "beta": 10.0, "d_cutoff": 1.0},
        "abs_scale": [1.0, 1.0],
        "deadzone": 0.0015,
        "relative": {
            "output": "auto",
            "trackball_counts": [0, 0],
            "trackball_mouse_speed": None,
            "gain_slow": 0.05,
            "gain_fast": 0.4,
            "speed_slow": 0.6,
            "speed_fast": 5.0,
            "min_speed": 0.15,
            "screen_aspect": "auto",
            "mouse_px_per_screen": 1920,
            "filter": {"min_cutoff": 1.0, "beta": 4.0, "d_cutoff": 1.0},
        },
        "click_freeze_ms": 180,
    },
    "scroll": {
        "notches_per_height": 25,
        "direction": "natural",
        "deadzone": 0.0015,
        "settle_ms": 200,
        "fling": {
            "enabled": True,
            "min_speed": 0.8,
            "boost": 2.0,
            "boost_ramp_s": 0.15,
            "time_constant_s": 0.45,
            "max_notches_per_s": 300,
            "stack": {
                "enabled": True,
                "window_s": 1.0,
                "factor": 1.0,
                "settle_ms": 60,
            },
        },
        "filter": {"min_cutoff": 1.0, "beta": 5.0, "d_cutoff": 1.0},
    },
    "zoom": {
        "enabled": True,
        "method": "touchpad",
        "notch_ratio": 1.15,
        "invert": False,
        "settle_ms": 150,
        "deadzone": 0.004,
        "ctrl_release_ms": 400,
        "filter": {"min_cutoff": 1.0, "beta": 2.0, "d_cutoff": 1.0},
        "touchpad": {
            "gain": 1.0,
            "start_mm": 45,
            "width_mm": 100,
            "height_mm": 65,
        },
    },
    "double_click": {
        "enabled": True,
        "window_ms": 700,
        "max_move": 0.02,
        "os_time_ms": "auto",
    },
    "pause": {
        "fist_hold_s": 1.0,
        "start_paused": False,
    },
    "safety": {
        "lost_hand_release_frames": 8,
    },
    "preview": {
        "enabled": True,
        "window_name": "gesture-control",
        "show_landmarks": True,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ValueError(f"{path.name}{where}: {problem}. Check the indentation "
                         "(2 spaces per level, no tabs) and the colons.") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _unknown_keys(data: dict[str, Any], ref: dict[str, Any], prefix: str = "") -> list[str]:
    """Setting names in data that don't exist in ref (usually typos)."""
    out = []
    for key, value in data.items():
        name = f"{prefix}{key}"
        if key not in ref:
            out.append(name)
        elif isinstance(value, dict) and isinstance(ref[key], dict):
            out += _unknown_keys(value, ref[key], name + ".")
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load DEFAULTS, then gestures.yaml, then calibration.yaml and local.yaml
    from the same folder (if present). Missing keys fall back to defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = copy.deepcopy(DEFAULTS)
    warnings: list[str] = []
    files = []
    if cfg_path.exists():
        files.append(cfg_path)
    elif path:
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    loaded = []
    for name in (CALIBRATION_FILE, LOCAL_FILE):
        extra = cfg_path.parent / name
        if extra.exists():
            files.append(extra)
            loaded.append(name)
    for f in files:
        data = _read_yaml(f)
        sc = data.get("scroll")
        fl = sc.get("fling") if isinstance(sc, dict) else None
        if isinstance(fl, dict) and "lift_frames" in fl:  # no longer used
            fl.pop("lift_frames")
            warnings.append(f"{f.name}: scroll.fling.lift_frames is no longer needed "
                            "(the glide now starts on the first frame) - ignored")
        if isinstance(sc, dict) and "invert" in sc:  # replaced by scroll.direction
            inverted = bool(sc.pop("invert"))
            sc.setdefault("direction", "natural" if inverted else "traditional")
            warnings.append(f"{f.name}: scroll.invert is replaced by scroll.direction "
                            f"(using direction: {sc['direction']})")
        for key in _unknown_keys(data, DEFAULTS):
            warnings.append(f"{f.name}: unknown setting '{key}' (typo?) - ignored")
        cfg = _deep_merge(cfg, data)
    cfg["_loaded"] = loaded
    cfg["_warnings"] = warnings

    model = Path(cfg["tracker"]["model"]).expanduser()
    if not model.is_absolute():
        model = PROJECT_ROOT / model
    cfg["tracker"]["model"] = str(model)

    if cfg["cursor"]["mode"] not in ("absolute", "relative"):
        raise ValueError("cursor.mode must be 'absolute' or 'relative'")
    boost = cfg["scroll"]["fling"]["boost"]
    if not isinstance(boost, (int, float)) or isinstance(boost, bool) or not 0.1 <= boost <= 10:
        raise ValueError("scroll.fling.boost must be a number between 0.1 and 10")
    _check_ranges(cfg)
    if cfg["zoom"]["method"] not in ("touchpad", "ctrl_scroll", "keys"):
        raise ValueError("zoom.method must be 'touchpad', 'ctrl_scroll' or 'keys'")
    tpc = cfg["zoom"]["touchpad"]
    if float(tpc["width_mm"]) < 60 or float(tpc["height_mm"]) < 40:
        raise ValueError("zoom.touchpad.width_mm must be at least 60 and height_mm at least 40")
    if not 20 <= float(tpc["start_mm"]) <= 70:
        raise ValueError("zoom.touchpad.start_mm must be between 20 and 70")
    nr = cfg["zoom"]["notch_ratio"]
    if not isinstance(nr, (int, float)) or isinstance(nr, bool) or not 1.01 <= nr <= 3:
        raise ValueError("zoom.notch_ratio must be a number between 1.01 and 3")
    if cfg["scroll"]["direction"] not in ("natural", "traditional"):
        raise ValueError("scroll.direction must be 'natural' or 'traditional'")
    if cfg["cursor"]["relative"]["output"] not in ("auto", "trackball", "tablet", "mouse"):
        raise ValueError("cursor.relative.output must be auto, trackball, tablet or mouse")
    tc = cfg["cursor"]["relative"]["trackball_counts"]
    if not (isinstance(tc, (list, tuple)) and len(tc) == 2
            and all(isinstance(v, (int, float)) and v >= 0 for v in tc)):
        raise ValueError("cursor.relative.trackball_counts must be two numbers >= 0 "
                         "(0 = automatic; set by --calibrate-trackball)")
    if cfg["cursor"]["anchor"] not in ("knuckles", "index_mcp", "index_tip", "palm_center"):
        raise ValueError("cursor.anchor must be knuckles, index_mcp, index_tip or palm_center")
    scale = cfg["cursor"]["abs_scale"]
    if (not isinstance(scale, (list, tuple)) or len(scale) != 2
            or not all(isinstance(v, (int, float)) and 0.1 <= v <= 1.0 for v in scale)):
        raise ValueError("cursor.abs_scale must be two numbers between 0.1 and 1.0")
    cfg["cursor"]["abs_scale"] = [float(scale[0]), float(scale[1])]
    if cfg["double_click"]["os_time_ms"] == "auto":
        cfg["double_click"]["os_time_ms"] = gnome_double_click_ms()
    return cfg


def gnome_double_click_ms(default: int = 400) -> int:
    """GNOME's double-click time setting (Settings > Mouse & Touchpad), in ms."""
    import shutil
    import subprocess

    if not shutil.which("gsettings"):
        return default
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.peripherals.mouse", "double-click"],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout.strip().split()[-1]
        return int(out)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return default


def _check_ranges(cfg: dict[str, Any]) -> None:
    """Catch values that would make the app misbehave (not just feel different)."""
    def num(path: str) -> float:
        node: Any = cfg
        for part in path.split("."):
            node = node[part]
        if isinstance(node, bool) or not isinstance(node, (int, float)):
            raise ValueError(f"{path} must be a number (got {node!r})")
        return float(node)

    def positive(*paths: str) -> None:
        for p in paths:
            if num(p) <= 0:
                raise ValueError(f"{p} must be greater than 0 (got {num(p)})")

    positive("pinch.start", "pinch.release", "pose.extend_ratio", "pose.curl_ratio",
             "cursor.relative.gain_slow", "cursor.relative.gain_fast",
             "scroll.notches_per_height",
             "scroll.fling.time_constant_s", "scroll.fling.boost",
             "scroll.fling.max_notches_per_s", "pause.fist_hold_s",
             "zoom.touchpad.gain")
    sa = cfg["cursor"]["relative"]["screen_aspect"]
    if sa != "auto":
        positive("cursor.relative.screen_aspect")
    if num("pinch.release") <= num("pinch.start"):
        raise ValueError(
            f"pinch.release ({num('pinch.release')}) must be larger than pinch.start "
            f"({num('pinch.start')}); otherwise a resting thumb clicks over and over")
    if num("pose.extend_ratio") <= num("pose.curl_ratio"):
        raise ValueError(
            f"pose.extend_ratio ({num('pose.extend_ratio')}) must be larger than "
            f"pose.curl_ratio ({num('pose.curl_ratio')})")
    if num("cursor.relative.speed_fast") <= num("cursor.relative.speed_slow"):
        raise ValueError("cursor.relative.speed_fast must be larger than speed_slow")
    for p in ("pose.debounce_frames", "pinch.confirm_frames"):
        if num(p) < 1:
            raise ValueError(f"{p} must be at least 1")
    for p in ("pinch.min_facing", "scroll.fling.stack.factor"):
        if not 0 <= num(p) <= 1.5:
            raise ValueError(f"{p} must be between 0 and 1.5")
    r = cfg["cursor"]["region"]
    if not (0 <= r["x_min"] < r["x_max"] <= 1 and 0 <= r["y_min"] < r["y_max"] <= 1):
        raise ValueError("cursor.region must have 0 <= x_min < x_max <= 1 and "
                         "0 <= y_min < y_max <= 1")


def pointer_device_mode(cfg: dict[str, Any]) -> str:
    """Which virtual device the cursor output needs: relative events for the mouse and
    trackball outputs, absolute positions for the tablet output and absolute mode.
    (cursor.relative.output must already be resolved from "auto".)"""
    cc = cfg["cursor"]
    if cc["mode"] == "relative" and cc["relative"]["output"] in ("mouse", "trackball"):
        return "relative"
    return "absolute"


def update_calibration(updates: dict[str, Any], config_path: str | Path | None = None) -> Path:
    """Merge `updates` into calibration.yaml next to the main config, keeping whatever
    else it holds (screen-edge and trackball calibration live side by side)."""
    folder = (Path(config_path) if config_path else DEFAULT_CONFIG_PATH).parent
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / CALIBRATION_FILE
    data: dict[str, Any] = {}
    if out.exists():
        try:
            data = yaml.safe_load(out.read_text()) or {}
        except yaml.YAMLError:
            data = {}
    data = _deep_merge(data, updates)
    out.write_text(
        "# Written by: gesture-control --calibrate / --calibrate-trackball\n"
        "# cursor.abs_scale: where the cursor reaches the right / bottom screen edge.\n"
        "# cursor.relative.trackball_counts: movement units across the screen.\n"
        "# Delete this file (or a key) to undo a calibration.\n"
        + yaml.safe_dump(data, sort_keys=False, default_flow_style=None)
    )
    return out


def save_calibration(abs_scale: tuple[float, float], config_path: str | Path | None = None) -> Path:
    """Write cursor.abs_scale (screen-edge calibration) to calibration.yaml."""
    return update_calibration({"cursor": {"abs_scale": [round(float(abs_scale[0]), 4),
                                                       round(float(abs_scale[1]), 4)]}},
                              config_path)
