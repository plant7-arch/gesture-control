"""Configuration: built-in defaults merged with config/gestures.yaml.

Where the files live depends on how the app is run:
  * from the project folder (`uv run gesture-control`): config/ and models/ in that
    folder, as always;
  * installed as a command (`uv tool install .`, then `gesture-control` anywhere):
    there is no project folder, so your settings go in ~/.config/gesture-control/
    (local.yaml, calibration.yaml) and the downloaded model in
    ~/.cache/gesture-control/models/. The defaults are built in, so no gestures.yaml
    is needed there.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

APP = "gesture-control"
# src/gesture_control/config.py -> project root is two levels above the package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def is_checkout(root: Path) -> bool:
    """Is root this project's source folder (not, say, an installed package's
    site-packages, whose grandparent is lib/pythonX.Y)?"""
    return (root / "pyproject.toml").is_file() and (root / "config" / "gestures.yaml").is_file()


def _xdg(var: str, fallback: str, env: dict[str, str], home: Path) -> Path:
    value = env.get(var, "")
    return Path(value) if value and Path(value).is_absolute() else home / fallback


def locate(root: Path = PROJECT_ROOT, env: dict[str, str] | None = None,
           home: Path | None = None) -> tuple[bool, Path, Path]:
    """(running from the project folder?, config folder, folder that models/ is in)."""
    if is_checkout(root):
        return True, root / "config", root
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else home
    return (False, _xdg("XDG_CONFIG_HOME", ".config", env, home) / APP,
            _xdg("XDG_CACHE_HOME", ".cache", env, home) / APP)


FROM_CHECKOUT, CONFIG_DIR, DATA_ROOT = locate()
DEFAULT_CONFIG_PATH = CONFIG_DIR / "gestures.yaml"


def command() -> str:
    """How to start the app, for messages: `uv run gesture-control` in the project
    folder, plain `gesture-control` when installed as a command."""
    return f"uv run {APP}" if FROM_CHECKOUT else APP


def shown(name: str, folder: Path | None = None) -> str:
    """How to name a config file in messages: config/local.yaml in the project
    folder, ~/.config/gesture-control/local.yaml when installed."""
    folder = CONFIG_DIR if folder is None else folder
    if FROM_CHECKOUT and folder == CONFIG_DIR:
        return f"config/{name}"
    path = folder / name
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)

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


def _check_sections(data: dict[str, Any], ref: dict[str, Any], fname: str,
                    warnings: list[str], prefix: str = "") -> None:
    """Sections (settings that hold other settings) must be mappings.

    A section with nothing under it (`pinch:` with every line below commented out)
    reads as null: it's dropped with a warning, so the defaults stay. Anything else
    in place of a section (`cursor: 5`, a list) is an error."""
    for key in list(data):
        value, default = data[key], ref.get(key)
        if not isinstance(default, dict):
            continue
        name = f"{prefix}{key}"
        if value is None:
            del data[key]
            warnings.append(f"{fname}: '{name}' has no settings under it - ignored")
        elif not isinstance(value, dict):
            raise ValueError(f"{fname}: '{name}' must be a section with settings indented "
                             f"under it (got {value!r})")
        else:
            _check_sections(value, default, fname, warnings, name + ".")


# Settings that take the word "auto" or a number.
AUTO_OR_NUMBER = {"cursor.relative.screen_aspect", "double_click.os_time_ms"}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_types(cfg: dict[str, Any], ref: dict[str, Any] = DEFAULTS, prefix: str = "") -> None:
    """Each setting must have the kind of value its default has. Without this, a
    quoted "false" would count as true and a quoted "0.2" would crash a comparison.
    Lists and choice words have their own checks in load_config."""
    for key, default in ref.items():
        name, value = f"{prefix}{key}", cfg[key]
        if isinstance(default, dict):
            _check_types(value, default, name + ".")
        elif name in AUTO_OR_NUMBER:
            if value != "auto" and not _is_number(value):
                raise ValueError(f"{name} must be auto or a number (got {value!r})")
        elif default is None:  # optional number, e.g. trackball_mouse_speed
            if value is not None and not _is_number(value):
                raise ValueError(f"{name} must be a number or null (got {value!r})")
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false, without quotes "
                                 f"(got {value!r})")
        elif _is_number(default):
            if not _is_number(value):
                raise ValueError(f"{name} must be a number (got {value!r})")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a word or text (got {value!r})")


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
        _check_sections(data, DEFAULTS, f.name, warnings)
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
    cfg["_config_dir"] = str(cfg_path.parent)
    cfg["_warnings"] = warnings

    _check_types(cfg)
    model = Path(cfg["tracker"]["model"]).expanduser()
    if not model.is_absolute():
        model = DATA_ROOT / model  # the project folder, or ~/.cache/gesture-control
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

    def non_negative(*paths: str) -> None:
        for p in paths:
            if num(p) < 0:
                raise ValueError(f"{p} must be 0 or more (got {num(p)})")

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
    # Counts and sizes: whole numbers, at least this much.
    for p, low in (("camera.index", 0), ("camera.width", 1), ("camera.height", 1),
                   ("tracker.num_hands", 1), ("tracker.second_hand_check_every", 0),
                   ("pose.debounce_frames", 1), ("pinch.confirm_frames", 1),
                   ("safety.lost_hand_release_frames", 1)):
        v = num(p)
        if v != int(v) or v < low:
            raise ValueError(f"{p} must be a whole number of at least {low} (got {v:g})")
    positive("camera.fps", "cursor.relative.mouse_px_per_screen")
    if cfg["double_click"]["os_time_ms"] != "auto":
        positive("double_click.os_time_ms")
    # One Euro filters: a cutoff of 0 divides by zero; a negative beta inverts them.
    for f in ("cursor.filter", "cursor.relative.filter", "scroll.filter", "zoom.filter"):
        positive(f"{f}.min_cutoff", f"{f}.d_cutoff")
        non_negative(f"{f}.beta")
    non_negative("cursor.deadzone", "scroll.deadzone", "zoom.deadzone",
                 "cursor.click_freeze_ms", "cursor.relative.min_speed",
                 "scroll.settle_ms", "scroll.fling.min_speed", "scroll.fling.boost_ramp_s",
                 "scroll.fling.stack.window_s", "scroll.fling.stack.settle_ms",
                 "zoom.settle_ms", "zoom.ctrl_release_ms", "double_click.window_ms",
                 "double_click.max_move", "pinch.min_finger_reach", "pinch.right_min_reach")
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


def update_calibration(updates: dict[str, Any], config_path: str | Path | None = None,
                       log: Callable[[str], None] = print) -> Path:
    """Merge `updates` into calibration.yaml next to the main config, keeping whatever
    else it holds (screen-edge and trackball calibration live side by side).

    A file that can't be read as settings is kept as calibration.yaml.bak (and the
    user told), never silently dropped. The new file is written to a temporary file
    and then moved into place, so an interrupted write can't leave half a file."""
    folder = (Path(config_path) if config_path else DEFAULT_CONFIG_PATH).parent
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / CALIBRATION_FILE
    data: Any = {}
    if out.exists():
        try:
            data = yaml.safe_load(out.read_text()) or {}
        except yaml.YAMLError:
            data = None
        if not isinstance(data, dict):
            backup = out.with_name(out.name + ".bak")
            os.replace(out, backup)
            log(f"{out.name} couldn't be read as settings; kept it as {backup.name} and "
                "wrote a new one. Copy any values you still need from the backup.")
            data = {}
    data = _deep_merge(data, updates)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(
            "# Written by: gesture-control --calibrate / --calibrate-trackball\n"
            "# cursor.abs_scale: where the cursor reaches the right / bottom screen edge.\n"
            "# cursor.relative.trackball_counts: movement units across the screen.\n"
            "# Delete this file (or a key) to undo a calibration.\n"
            + yaml.safe_dump(data, sort_keys=False, default_flow_style=None)
        )
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def save_calibration(abs_scale: tuple[float, float], config_path: str | Path | None = None) -> Path:
    """Write cursor.abs_scale (screen-edge calibration) to calibration.yaml."""
    return update_calibration({"cursor": {"abs_scale": [round(float(abs_scale[0]), 4),
                                                       round(float(abs_scale[1]), 4)]}},
                              config_path)
