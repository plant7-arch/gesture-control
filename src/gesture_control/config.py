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
        "num_hands": 1,
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
        "confirm_frames": 2,
        "min_finger_reach": 1.05,
    },
    "cursor": {
        "mode": "absolute",
        "anchor": "index_mcp",
        "region": {"x_min": 0.2, "x_max": 0.8, "y_min": 0.2, "y_max": 0.7},
        "filter": {"min_cutoff": 1.0, "beta": 10.0, "d_cutoff": 1.0},
        "abs_scale": [1.0, 1.0],
        "deadzone": 0.0015,
        "relative_gain": 1800,
        "click_freeze_ms": 180,
    },
    "scroll": {
        "notches_per_height": 25,
        "invert": False,
        "deadzone": 0.0015,
        "settle_ms": 200,
        "filter": {"min_cutoff": 1.0, "beta": 5.0, "d_cutoff": 1.0},
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
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load DEFAULTS, then gestures.yaml, then calibration.yaml and local.yaml
    from the same folder (if present). Missing keys fall back to defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = copy.deepcopy(DEFAULTS)
    if cfg_path.exists():
        cfg = _deep_merge(cfg, _read_yaml(cfg_path))
    elif path:
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    loaded = []
    for name in (CALIBRATION_FILE, LOCAL_FILE):
        extra = cfg_path.parent / name
        if extra.exists():
            cfg = _deep_merge(cfg, _read_yaml(extra))
            loaded.append(name)
    cfg["_loaded"] = loaded

    model = Path(cfg["tracker"]["model"]).expanduser()
    if not model.is_absolute():
        model = PROJECT_ROOT / model
    cfg["tracker"]["model"] = str(model)

    if cfg["cursor"]["mode"] not in ("absolute", "relative"):
        raise ValueError("cursor.mode must be 'absolute' or 'relative'")
    scale = cfg["cursor"]["abs_scale"]
    if (not isinstance(scale, (list, tuple)) or len(scale) != 2
            or not all(isinstance(v, (int, float)) and 0.1 <= v <= 1.0 for v in scale)):
        raise ValueError("cursor.abs_scale must be two numbers between 0.1 and 1.0")
    cfg["cursor"]["abs_scale"] = [float(scale[0]), float(scale[1])]
    return cfg


def save_calibration(abs_scale: tuple[float, float], config_path: str | Path | None = None) -> Path:
    """Write cursor.abs_scale to calibration.yaml next to the main config."""
    folder = (Path(config_path) if config_path else DEFAULT_CONFIG_PATH).parent
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / CALIBRATION_FILE
    out.write_text(
        "# Written by: gesture-control --calibrate\n"
        "# Fraction of the virtual mouse range where the cursor reaches the right / bottom\n"
        "# edge of the screen. Delete this file to undo the calibration.\n"
        "cursor:\n"
        f"  abs_scale: [{abs_scale[0]:.4f}, {abs_scale[1]:.4f}]\n"
    )
    return out
