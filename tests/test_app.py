"""Main-loop helpers: which hand drives the cursor, and spotting hand switches."""

from types import SimpleNamespace

import numpy as np
import pytest

from gesture_control.app import MAX_JUMP_PALMS, _is_switch, _pick_primary, parse_args


def _hand(x, y):
    pts = np.zeros((21, 3))
    pts[:, 0], pts[:, 1] = x, y
    return SimpleNamespace(image=pts)


def test_single_hand_is_primary():
    h = _hand(0.5, 0.5)
    assert _pick_primary([h], (0.1, 0.1), "knuckles") is h
    assert _pick_primary([], None, "knuckles") is None


def test_second_hand_does_not_steal_the_cursor():
    """The tracker lists the most confident hand first; the primary hand is the one
    nearest to where the cursor hand was, whatever the order."""
    mine, newcomer = _hand(0.3, 0.5), _hand(0.7, 0.5)
    assert _pick_primary([newcomer, mine], (0.31, 0.5), "knuckles") is mine
    assert _pick_primary([mine, newcomer], (0.31, 0.5), "knuckles") is mine


def test_without_history_the_most_confident_hand_wins():
    a, b = _hand(0.3, 0.5), _hand(0.7, 0.5)
    assert _pick_primary([b, a], None, "knuckles") is b


PALM = 0.1


def test_normal_movement_is_not_a_switch():
    assert not _is_switch((0.52, 0.5), (0.5, 0.5), None, PALM)


def test_impossible_jump_is_a_switch():
    jump = (MAX_JUMP_PALMS + 0.2) * PALM
    assert _is_switch((0.5 + jump, 0.5), (0.5, 0.5), None, PALM)


def test_following_the_other_hand_is_a_switch():
    # Now nearer to where the *other* hand was than to our own last position.
    assert _is_switch((0.69, 0.5), (0.5, 0.5), (0.7, 0.5), PALM)


@pytest.mark.parametrize("anchor, prev", [(None, (0.5, 0.5)), ((0.5, 0.5), None)])
def test_no_history_is_not_a_switch(anchor, prev):
    assert not _is_switch(anchor, prev, (0.9, 0.9), PALM)


def test_unknown_palm_size_skips_the_jump_check():
    assert not _is_switch((0.9, 0.5), (0.1, 0.5), None, None)


def test_cli_flags():
    a = parse_args(["--dry-run", "--mode", "absolute", "--camera", "2", "-v"])
    assert a.dry_run and a.verbose and a.mode == "absolute" and a.camera == 2
    with pytest.raises(SystemExit):
        parse_args(["--mode", "sideways"])


def test_version_has_one_source():
    """pyproject.toml holds the version; the package, the lock file and --version agree."""
    import tomllib
    from pathlib import Path

    import gesture_control
    from gesture_control.config import PROJECT_ROOT

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]
    assert gesture_control.__version__ == project["version"]
    lock = tomllib.loads(Path(PROJECT_ROOT / "uv.lock").read_text())
    locked = next(p for p in lock["package"] if p["name"] == "gesture-control")
    assert locked["version"] == project["version"]
    assert "Add your description" not in project["description"]


def test_version_flag(capsys):
    import gesture_control

    with pytest.raises(SystemExit) as exc:
        parse_args(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"gesture-control {gesture_control.__version__}"


@pytest.mark.parametrize("text, message", [
    ("cursor:\n  region:\n    x_min: \"0.2\"\n", "cursor.region.x_min must be a number"),
    ("cursor: 5\n", "'cursor' must be a section"),
])
def test_bad_config_exits_with_a_message_not_a_traceback(tmp_path, text, message):
    import shutil

    from gesture_control.app import main
    from gesture_control.config import DEFAULT_CONFIG_PATH

    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    (tmp_path / "local.yaml").write_text(text)
    with pytest.raises(SystemExit) as exc:
        main(["--config", str(tmp_path / "gestures.yaml"), "--dry-run", "--no-preview"])
    assert str(exc.value.code).startswith("Config error:") and message in str(exc.value.code)


def test_unexpected_type_error_while_loading_is_still_a_config_error(monkeypatch):
    from gesture_control import app

    def broken(_path):
        raise TypeError("'<' not supported between instances of 'str' and 'float'")

    monkeypatch.setattr(app, "load_config", broken)
    with pytest.raises(SystemExit) as exc:
        app.main(["--dry-run", "--no-preview"])
    assert str(exc.value.code).startswith("Config error:")
