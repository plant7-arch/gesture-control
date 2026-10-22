"""v0.20.0: the command line and the config files are equally powerful. Every setting
is also an option (pinch.start -> --pinch-start), read like its value in local.yaml,
checked the same way, and winning over the files; the old short options stay."""

from __future__ import annotations

import shutil

import pytest
import yaml

from gesture_control import app
from gesture_control.app import SHORTCUTS, parse_args
from gesture_control.config import (
    DEFAULT_CONFIG_PATH,
    default_of,
    load_config,
    option_name,
    setting_names,
)

ALL = setting_names()


@pytest.fixture
def cfgdir(tmp_path):
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    return tmp_path


def _load(folder, argv=(), local=""):
    if local:
        (folder / "local.yaml").write_text(local)
    args = parse_args(list(argv))
    return load_config(folder / "gestures.yaml", args.overrides), args


def _clean(cfg):
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


def _as_local_yaml(name, value) -> str:
    node = value
    for part in reversed(name.split(".")):
        node = {part: node}
    return yaml.safe_dump(node, default_flow_style=False)


def test_naming_rule():
    assert option_name("pinch.start") == "--pinch-start"
    assert option_name("cursor.relative.gain_slow") == "--cursor-relative-gain-slow"
    assert option_name("zoom.touchpad.start_mm") == "--zoom-touchpad-start-mm"
    assert option_name("run.dry_run") == "--run-dry-run"


def test_every_setting_has_an_option_listed_in_help(capsys):
    assert len(ALL) >= 104
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    text = capsys.readouterr().out
    missing = [option_name(n) for n in ALL if option_name(n) not in text]
    assert missing == []


@pytest.mark.parametrize("name", ALL)
def test_each_option_sets_exactly_what_local_yaml_would(cfgdir, name):
    """For every setting: the same value given on the command line or in local.yaml
    gives the same configuration. Values are the defaults written out as text (they
    are always valid), so lists, null, auto and words are all covered."""
    from gesture_control.app import yaml_text

    value = default_of(name)
    from_cli, _ = _load(cfgdir, [option_name(name), yaml_text(value)])
    (cfgdir / "local.yaml").unlink(missing_ok=True)
    from_file, _ = _load(cfgdir, local=_as_local_yaml(name, value))
    assert _clean(from_cli) == _clean(from_file)
    assert from_cli["_cli"] == [name]


@pytest.mark.parametrize("argv, setting, value", [
    (["--pinch-start", "0.32"], "pinch.start", 0.32),
    (["--phone-facing", "front"], "phone.facing", "front"),
    (["--cursor-relative-gain-slow", "0.03"], "cursor.relative.gain_slow", 0.03),
    (["--preview-enabled", "false"], "preview.enabled", False),
    (["--cursor-abs-scale", "[0.9, 0.95]"], "cursor.abs_scale", [0.9, 0.95]),
    (["--phone-extra-args", "[--camera-ar=4:3]"], "phone.extra_args", ["--camera-ar=4:3"]),
    (["--double-click-os-time-ms", "auto"], "double_click.os_time_ms", 400),  # resolved
    (["--cursor-relative-trackball-mouse-speed", "-0.25"],
     "cursor.relative.trackball_mouse_speed", -0.25),             # a negative number
    (["--phone-bit-rate", "8000000"], "phone.bit_rate", "8000000"),
])
def test_values_are_read_like_local_yaml(cfgdir, argv, setting, value):
    cfg, _ = _load(cfgdir, argv)
    node = cfg
    for part in setting.split("."):
        node = node[part]
    assert node == value


def test_the_command_line_wins_over_local_yaml(cfgdir):
    cfg, _ = _load(cfgdir, ["--pinch-start", "0.3"],
                   local="pinch:\n  start: 0.25\n  release: 0.45\n")
    assert cfg["pinch"]["start"] == 0.3 and cfg["pinch"]["release"] == 0.45


@pytest.mark.parametrize("argv, message", [
    (["--pinch-start", "0.5"], r"pinch.release \(0.42\) must be larger than pinch.start"),
    (["--phone-facing", "side"], "phone.facing must be back, front or external"),
    (["--camera-fourcc", "MJPEG"], "camera.fourcc must be 4 characters"),
    (["--pinch-start", "fast"], "pinch.start must be a number"),
    (["--preview-enabled", "maybe"], "preview.enabled must be true or false"),
])
def test_bad_values_get_the_same_checks_as_the_files(cfgdir, argv, message):
    with pytest.raises(ValueError, match=message):
        _load(cfgdir, argv)


def test_the_app_names_the_option_behind_a_config_error(capsys):
    with pytest.raises(SystemExit) as exc:
        app.main(["--pointer-test", "--dry-run", "--pinch-start", "0.5"])
    assert "(from the command line: --pinch-start 0.5)" in str(exc.value.code)


@pytest.mark.parametrize("argv, message", [
    (["--pha", "front"], "unrecognized arguments: --pha"),        # no abbreviations
    (["--pinch-st", "0.3"], "unrecognized arguments: --pinch-st"),
    (["--cursor-abs-scale", "[0.9,"], "argument --cursor-abs-scale: can't read"),
    (["--camera", "2", "--camera-index", "3"], "disagree; give camera.index once"),
    (["--phone", "--phone-enabled", "false"], "disagree; give phone.enabled once"),
    (["--connect", "192.168.1.6:40635", "--phone-connect", "192.168.1.7:5555"],
     "disagree; give phone.connect once"),
    (["--dry-run", "--run-dry-run", "false"], "disagree; give run.dry_run once"),
])
def test_command_line_mistakes_are_refused(capsys, argv, message):
    with pytest.raises(SystemExit):
        parse_args(argv)
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("short, long", [
    (["--camera", "2"], ["--camera-index", "2"]),
    (["--mode", "absolute"], ["--cursor-mode", "absolute"]),
    (["--no-preview"], ["--preview-enabled", "false"]),
    (["--start-paused"], ["--pause-start-paused", "true"]),
    (["--phone"], ["--phone-enabled", "true"]),
    (["--no-phone"], ["--phone-enabled", "false"]),
    (["--connect", "192.168.1.6:40635"],
     ["--phone-connect", "192.168.1.6:40635", "--phone-enabled", "true"]),
    (["--dry-run"], ["--run-dry-run", "true"]),
    (["-v"], ["--run-verbose", "true"]),
])
def test_short_options_and_their_long_forms_agree(cfgdir, short, long):
    a, _ = _load(cfgdir, short)
    b, _ = _load(cfgdir, long)
    assert _clean(a) == _clean(b)
    _load(cfgdir, short + long)          # both, with the same value: fine


def test_every_shortcut_names_a_real_setting():
    assert set(SHORTCUTS) <= set(ALL)


def test_run_settings_work_from_local_yaml(monkeypatch, tmp_path, capsys):
    """run.dry_run in local.yaml, no --dry-run: the app still only prints."""
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text("run:\n  dry_run: true\n")
    seen = {}

    def stop_after(cfg, dry_run, *a, **k):
        seen["dry_run"] = dry_run
        raise SystemExit(0)

    monkeypatch.setattr(app, "_resolve_output", stop_after)
    with pytest.raises(SystemExit):
        app.main(["--config", str(tmp_path / "gestures.yaml"), "--pointer-test"])
    assert seen["dry_run"] is True



def test_every_option_is_in_the_readme():
    """README > All settings has an Option column: every setting's option is in it."""
    from gesture_control.config import PROJECT_ROOT

    readme = (PROJECT_ROOT / "README.md").read_text()
    missing = [option_name(n) for n in ALL if f"`{option_name(n)}`" not in readme]
    assert missing == []
