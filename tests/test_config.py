"""Config layering (defaults < gestures.yaml < calibration.yaml < local.yaml) and the
checks that refuse values which would make the app misbehave."""

import shutil
from pathlib import Path

import pytest
import yaml

from gesture_control.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULTS,
    PROJECT_ROOT,
    _unknown_keys,
    load_config,
    pointer_device_mode,
    save_calibration,
    update_calibration,
)


@pytest.fixture
def cfgdir(tmp_path):
    """A config folder holding only the shipped gestures.yaml (never the user's
    local.yaml or calibration.yaml)."""
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    return tmp_path


def _write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text)


def _load(folder: Path):
    return load_config(folder / "gestures.yaml")


# -- the shipped reference file ----------------------------------------------------

def test_gestures_yaml_lists_every_setting_with_the_default_value():
    """gestures.yaml is the documented reference copy: same keys, same values."""
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    assert _unknown_keys(data, DEFAULTS) == [], "setting in gestures.yaml but not in DEFAULTS"
    assert _unknown_keys(DEFAULTS, data) == [], "setting in DEFAULTS but not in gestures.yaml"

    def values(a, b, prefix=""):
        for k, v in a.items():
            if isinstance(v, dict):
                yield from values(v, b[k], f"{prefix}{k}.")
            elif v != b[k]:
                yield f"{prefix}{k}: DEFAULTS {v!r} vs gestures.yaml {b[k]!r}"

    assert list(values(DEFAULTS, data)) == []


def test_every_setting_is_documented_in_the_readme():
    readme = (PROJECT_ROOT / "README.md").read_text()

    def leaves(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from leaves(v, f"{prefix}{k}.")
            else:
                yield prefix + k, k

    missing = [full for full, leaf in leaves(DEFAULTS) if f"`{leaf}`" not in readme
               and f"{leaf}`" not in readme and f".{leaf}" not in readme]
    assert missing == []


def test_shipped_config_loads_cleanly(cfgdir):
    cfg = _load(cfgdir)
    assert cfg["_warnings"] == []
    assert cfg["_loaded"] == []
    assert Path(cfg["tracker"]["model"]).is_absolute()
    assert cfg["double_click"]["os_time_ms"] == 400   # "auto" resolved


def test_explicit_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


# -- layering ------------------------------------------------------------------------

def test_layers_override_in_order(cfgdir):
    _write(cfgdir, "calibration.yaml", "pinch:\n  start: 0.30\n  release: 0.45\n")
    _write(cfgdir, "local.yaml", "pinch:\n  start: 0.32\n")
    cfg = _load(cfgdir)
    assert cfg["pinch"]["start"] == 0.32     # local wins over calibration
    assert cfg["pinch"]["release"] == 0.45   # calibration wins over gestures.yaml
    assert cfg["pinch"]["min_facing"] == 0.6  # untouched keys keep defaults
    assert cfg["_loaded"] == ["calibration.yaml", "local.yaml"]


def test_partial_section_keeps_sibling_defaults(cfgdir):
    _write(cfgdir, "local.yaml", "scroll:\n  fling:\n    boost: 3.0\n")
    cfg = _load(cfgdir)
    assert cfg["scroll"]["fling"]["boost"] == 3.0
    assert cfg["scroll"]["fling"]["stack"]["window_s"] == 1.0
    assert cfg["scroll"]["notches_per_height"] == 25


def test_empty_local_file_is_fine(cfgdir):
    _write(cfgdir, "local.yaml", "# nothing yet\n")
    assert _load(cfgdir)["_warnings"] == []


def test_relative_model_path_resolves_to_project(cfgdir):
    _write(cfgdir, "local.yaml", "tracker:\n  model: models/x.task\n")
    assert _load(cfgdir)["tracker"]["model"] == str(PROJECT_ROOT / "models" / "x.task")


# -- mistakes the user can make ---------------------------------------------------------

def test_typo_is_a_warning_not_an_error(cfgdir):
    _write(cfgdir, "local.yaml", "pinch:\n  strat: 0.3\n")
    cfg = _load(cfgdir)
    assert any("pinch.strat" in w for w in cfg["_warnings"])
    assert cfg["pinch"]["start"] == 0.28


def test_bad_yaml_names_the_file_and_line(cfgdir):
    _write(cfgdir, "local.yaml", "pinch:\n  start: 0.3\n  release:0.5: x: y\n")
    with pytest.raises(ValueError, match=r"local\.yaml \(line 3"):
        _load(cfgdir)


def test_top_level_must_be_a_mapping(cfgdir):
    _write(cfgdir, "local.yaml", "- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        _load(cfgdir)


@pytest.mark.parametrize("text, message", [
    ("pinch:\n  start: 0.5\n", "pinch.release"),               # release <= start
    ("pose:\n  extend_ratio: 0.9\n", "pose.extend_ratio"),     # extend <= curl
    ("cursor:\n  relative:\n    gain_slow: 0\n", "gain_slow"),
    ("cursor:\n  relative:\n    gain_fast: -1\n", "gain_fast"),
    ("pose:\n  debounce_frames: 0\n", "debounce_frames"),
    ("pinch:\n  confirm_frames: 0\n", "confirm_frames"),
    ("pinch:\n  start: yes\n", "pinch.start must be a number"),
    ("cursor:\n  mode: tablet\n", "cursor.mode"),
    ("cursor:\n  region:\n    x_min: 0.9\n", "cursor.region"),
    ("cursor:\n  abs_scale: [1.2, 1.0]\n", "abs_scale"),
    ("cursor:\n  anchor: wrist\n", "cursor.anchor"),
    ("cursor:\n  relative:\n    output: joystick\n", "output"),
    ("cursor:\n  relative:\n    trackball_counts: [100]\n", "trackball_counts"),
    ("cursor:\n  relative:\n    speed_fast: 0.5\n", "speed_fast"),
    ("scroll:\n  direction: up\n", "scroll.direction"),
    ("scroll:\n  fling:\n    boost: 50\n", "boost"),
    ("zoom:\n  method: magic\n", "zoom.method"),
    ("zoom:\n  notch_ratio: 1.0\n", "notch_ratio"),
    ("zoom:\n  touchpad:\n    start_mm: 90\n", "start_mm"),
    ("zoom:\n  touchpad:\n    width_mm: 30\n", "width_mm"),
    ("pinch:\n  min_facing: 2\n", "min_facing"),
])
def test_values_that_break_the_app_are_refused(cfgdir, text, message):
    _write(cfgdir, "local.yaml", text)
    with pytest.raises(ValueError, match=message):
        _load(cfgdir)


def test_legacy_scroll_invert_is_migrated(cfgdir):
    _write(cfgdir, "local.yaml", "scroll:\n  invert: false\n")
    cfg = _load(cfgdir)
    assert cfg["scroll"]["direction"] == "traditional"
    assert any("scroll.invert" in w for w in cfg["_warnings"])
    assert not any("unknown setting" in w for w in cfg["_warnings"])


def test_legacy_lift_frames_is_dropped(cfgdir):
    _write(cfgdir, "local.yaml", "scroll:\n  fling:\n    lift_frames: 2\n")
    cfg = _load(cfgdir)
    assert "lift_frames" not in cfg["scroll"]["fling"]
    assert any("lift_frames" in w for w in cfg["_warnings"])


# -- calibration file ---------------------------------------------------------------

def test_calibrations_are_merged_not_overwritten(cfgdir):
    cfg_path = cfgdir / "gestures.yaml"
    update_calibration({"cursor": {"relative": {"trackball_counts": [1536, 864]}}}, cfg_path)
    save_calibration((0.91234567, 0.85), cfg_path)
    data = yaml.safe_load((cfgdir / "calibration.yaml").read_text())
    assert data["cursor"]["abs_scale"] == [0.9123, 0.85]
    assert data["cursor"]["relative"]["trackball_counts"] == [1536, 864]
    cfg = _load(cfgdir)                       # and the app reads both back
    assert cfg["cursor"]["abs_scale"] == [0.9123, 0.85]
    assert cfg["_warnings"] == []


def test_broken_calibration_file_is_replaced(cfgdir):
    _write(cfgdir, "calibration.yaml", "cursor: [unclosed\n")
    save_calibration((0.9, 0.9), cfgdir / "gestures.yaml")
    assert _load(cfgdir)["cursor"]["abs_scale"] == [0.9, 0.9]


@pytest.mark.parametrize("mode, output, device", [
    ("relative", "trackball", "relative"),
    ("relative", "mouse", "relative"),
    ("relative", "tablet", "absolute"),
    ("absolute", "trackball", "absolute"),
])
def test_pointer_device_mode(mode, output, device):
    cfg = {"cursor": {"mode": mode, "relative": {"output": output}}}
    assert pointer_device_mode(cfg) == device
