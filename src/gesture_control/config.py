"""Configuration: built-in defaults merged with config/gestures.yaml."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

# src/gesture_control/config.py -> project root is two levels above the package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "gestures.yaml"

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


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load the YAML config on top of DEFAULTS. Missing keys fall back to defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = copy.deepcopy(DEFAULTS)
    if cfg_path.exists():
        with cfg_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{cfg_path} must contain a YAML mapping")
        cfg = _deep_merge(cfg, data)
    elif path:
        raise FileNotFoundError(f"Config file not found: {cfg_path}")

    model = Path(cfg["tracker"]["model"]).expanduser()
    if not model.is_absolute():
        model = PROJECT_ROOT / model
    cfg["tracker"]["model"] = str(model)

    if cfg["cursor"]["mode"] not in ("absolute", "relative"):
        raise ValueError("cursor.mode must be 'absolute' or 'relative'")
    return cfg
