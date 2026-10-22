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
    # v0.18.1: values that used to load but crashed or silently broke a feature
    ("camera:\n  fourcc: MJPEG\n", "camera.fourcc must be 4 characters"),  # crashed
    ("camera:\n  fourcc: MJ\n", "camera.fourcc must be 4 characters"),
    ("pinch:\n  min_facing: 1.2\n", "pinch.min_facing must be between 0 and 1"),
    ("tracker:\n  min_detection_confidence: 1.5\n", "min_detection_confidence must be"),
    ("tracker:\n  min_presence_confidence: -0.1\n", "min_presence_confidence must be"),
    ("tracker:\n  min_tracking_confidence: 2\n", "min_tracking_confidence must be"),
    # no room left to zoom in on the smallest pad
    ("zoom:\n  touchpad:\n    width_mm: 60\n    height_mm: 40\n    start_mm: 60\n",
     r"between 26 and 41 for a 60 x 40 mm touchpad"),
    ("zoom:\n  touchpad:\n    width_mm: 60\n    height_mm: 40\n",   # default start 45
     r"start_mm \(45\) must be between 26 and 41"),
    ("zoom:\n  touchpad:\n    start_mm: 21\n", "between 26 and 80"),  # no room to zoom out
    ("cursor:\n  relative:\n    trackball_counts: [true, true]\n", "trackball_counts"),
    ("cursor:\n  abs_scale: [true, true]\n", "abs_scale"),
])
def test_values_that_break_the_app_are_refused(cfgdir, text, message):
    _write(cfgdir, "local.yaml", text)
    with pytest.raises(ValueError, match=message):
        _load(cfgdir)


# -- empty sections and wrong kinds of values ---------------------------------------

@pytest.mark.parametrize("text, section", [
    ("pinch:\n  # start: 0.3\n", "pinch"),                       # everything commented out
    ("scroll:\n  fling:\n  # boost: 3\n", "scroll.fling"),        # nested
    ("cursor:\n  relative:\n    filter:\n", "cursor.relative.filter"),
])
def test_section_with_nothing_under_it_is_a_warning(cfgdir, text, section):
    """Commenting out every setting under a heading leaves an empty (null) section.
    It used to crash with a TypeError traceback; now the defaults stay."""
    _write(cfgdir, "local.yaml", text)
    cfg = _load(cfgdir)
    assert any(f"'{section}' has no settings under it" in w for w in cfg["_warnings"])
    node = cfg
    for part in section.split("."):
        node = node[part]
    assert isinstance(node, dict) and node      # defaults kept


@pytest.mark.parametrize("text, section", [
    ("cursor: 5\n", "'cursor' must be a section"),
    ("pinch: [0.3, 0.4]\n", "'pinch' must be a section"),
    ("scroll:\n  fling: fast\n", "'scroll.fling' must be a section"),
])
def test_value_in_place_of_a_section_is_refused(cfgdir, text, section):
    _write(cfgdir, "local.yaml", text)
    with pytest.raises(ValueError, match=section):
        _load(cfgdir)


@pytest.mark.parametrize("text, message", [
    # quoted values are text, not numbers or true/false
    ("cursor:\n  region:\n    x_min: \"0.2\"\n", "cursor.region.x_min must be a number"),
    ("preview:\n  enabled: \"false\"\n", "preview.enabled must be true or false"),
    ("camera:\n  mirror: 0\n", "camera.mirror must be true or false"),
    ("zoom:\n  enabled: off please\n", "zoom.enabled must be true or false"),
    ("double_click:\n  os_time_ms: fast\n", "os_time_ms must be auto or a number"),
    ("double_click:\n  os_time_ms: 0\n", "os_time_ms must be greater than 0"),
    ("cursor:\n  relative:\n    screen_aspect: wide\n", "screen_aspect must be auto or a number"),
    ("cursor:\n  relative:\n    trackball_mouse_speed: fast\n", "number or null"),
    ("tracker:\n  model: 5\n", "tracker.model must be a word or text"),
    # whole numbers
    ("tracker:\n  num_hands: 0\n", "num_hands must be a whole number of at least 1"),
    ("tracker:\n  num_hands: 1.5\n", "num_hands must be a whole number"),
    ("tracker:\n  second_hand_check_every: -1\n", "second_hand_check_every"),
    ("camera:\n  width: 0\n", "camera.width"),
    ("camera:\n  index: -1\n", "camera.index"),
    ("safety:\n  lost_hand_release_frames: 0\n", "lost_hand_release_frames"),
    ("camera:\n  buffers: 1\n", "camera.buffers must be a whole number of at least 2"),
    # ranges that break the maths
    ("camera:\n  fps: 0\n", "camera.fps"),
    ("scroll:\n  filter:\n    min_cutoff: 0\n", "scroll.filter.min_cutoff"),
    ("zoom:\n  filter:\n    beta: -1\n", "zoom.filter.beta"),
    ("cursor:\n  deadzone: -0.1\n", "cursor.deadzone must be 0 or more"),
    ("scroll:\n  fling:\n    stack:\n      window_s: -1\n", "stack.window_s"),
])
def test_wrong_kind_of_value_is_refused(cfgdir, text, message):
    _write(cfgdir, "local.yaml", text)
    with pytest.raises(ValueError, match=message):
        _load(cfgdir)


@pytest.mark.parametrize("text", [
    "double_click:\n  os_time_ms: 500\n",
    "cursor:\n  relative:\n    screen_aspect: 1.78\n",
    "cursor:\n  relative:\n    trackball_mouse_speed: null\n",
    "cursor:\n  relative:\n    trackball_mouse_speed: -0.25\n",
    "tracker:\n  num_hands: 1\n  second_hand_check_every: 0\n",
    "pinch:\n  right_min_reach: 0\n",
    "cursor:\n  region:\n    x_min: 0\n    x_max: 1\n",          # ints where floats are usual
    "camera:\n  width: 1280.0\n  height: 720\n",                 # 1280.0 is a whole number
    "camera:\n  fourcc: YUYV\n",
    "pinch:\n  min_facing: 1.0\n",
    "tracker:\n  min_detection_confidence: 0\n  min_tracking_confidence: 1\n",
    "zoom:\n  touchpad:\n    width_mm: 60\n    height_mm: 40\n    start_mm: 35\n",
    "zoom:\n  touchpad:\n    start_mm: 70\n",                    # default pad: room left
])
def test_valid_values_of_every_kind_still_load(cfgdir, text):
    _write(cfgdir, "local.yaml", text)
    assert _load(cfgdir)["_warnings"] == []


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


@pytest.mark.parametrize("broken", [
    "cursor: [unclosed\n",                 # not valid YAML
    "- a list\n- not settings\n",          # valid YAML, but not a mapping (used to crash)
])
def test_broken_calibration_file_is_kept_as_a_backup(cfgdir, broken):
    """A calibration file that can't be read is moved to calibration.yaml.bak (and
    the user told), not silently thrown away; a new file is written."""
    _write(cfgdir, "calibration.yaml", broken)
    said = []
    update_calibration({"cursor": {"abs_scale": [0.9, 0.9]}}, cfgdir / "gestures.yaml",
                       log=said.append)
    assert (cfgdir / "calibration.yaml.bak").read_text() == broken
    assert said and "calibration.yaml.bak" in said[0]
    assert _load(cfgdir)["cursor"]["abs_scale"] == [0.9, 0.9]


def test_calibration_is_written_whole_or_not_at_all(cfgdir, monkeypatch):
    """The new file goes to a temporary file first and is then moved into place:
    a failed write leaves the old calibration as it was, and no temp file behind."""
    cfg_path = cfgdir / "gestures.yaml"
    save_calibration((0.8, 0.8), cfg_path)
    before = (cfgdir / "calibration.yaml").read_text()

    import os

    def fail(*_a, **_k):
        raise OSError("disk full")       # the temporary file is written, the move fails

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        save_calibration((0.5, 0.5), cfg_path)
    assert (cfgdir / "calibration.yaml").read_text() == before
    assert sorted(p.name for p in cfgdir.iterdir()) == ["calibration.yaml", "gestures.yaml"]


def test_good_calibration_file_makes_no_backup(cfgdir):
    cfg_path = cfgdir / "gestures.yaml"
    save_calibration((0.8, 0.8), cfg_path)
    save_calibration((0.7, 0.7), cfg_path)
    assert not (cfgdir / "calibration.yaml.bak").exists()


@pytest.mark.parametrize("mode, output, device", [
    ("relative", "trackball", "relative"),
    ("relative", "mouse", "relative"),
    ("relative", "tablet", "absolute"),
    ("absolute", "trackball", "absolute"),
])
def test_pointer_device_mode(mode, output, device):
    cfg = {"cursor": {"mode": mode, "relative": {"output": output}}}
    assert pointer_device_mode(cfg) == device
