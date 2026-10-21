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


# -- stopping cleanly -------------------------------------------------------------------

def test_signal_before_cleanup_stops_like_ctrl_c():
    from gesture_control.app import ShutdownGuard

    g = ShutdownGuard(exit_now=lambda code: None, say=lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        g._handle(15, None)                       # SIGTERM (kill), SIGHUP, Ctrl+C


def test_repeated_signal_during_cleanup_doesnt_cut_it_short():
    """The first repeat only says it's still stopping; a second one quits at once."""
    from gesture_control.app import ShutdownGuard

    exits, said = [], []
    g = ShutdownGuard(exit_now=exits.append, say=said.append)
    g.cleaning()
    g._handle(2, None)                            # no exception: cleanup continues
    assert exits == [] and "Ctrl+C again" in said[0]
    g._handle(2, None)
    assert exits == [130]


def test_message_to_a_closed_terminal_is_ignored():
    """After SIGHUP the terminal may be gone; printing must not break the cleanup."""
    from gesture_control.app import ShutdownGuard

    def gone(_):
        raise OSError(5, "Input/output error")

    g = ShutdownGuard(exit_now=lambda code: None, say=gone)
    g.cleaning()
    g._handle(1, None)


def test_guard_installs_and_restores_the_signal_handlers():
    import signal

    from gesture_control.app import ShutdownGuard

    before = {s: signal.getsignal(s) for s in ShutdownGuard.SIGNALS}
    g = ShutdownGuard()
    g.install()
    assert all(signal.getsignal(s) == g._handle for s in ShutdownGuard.SIGNALS)
    g.restore()
    assert {s: signal.getsignal(s) for s in ShutdownGuard.SIGNALS} == before


def test_ctrl_c_in_a_test_mode_stops_without_a_traceback(monkeypatch, capsys):
    from gesture_control import app

    def interrupted(*_a):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "zoom_test", interrupted)
    app.main(["--zoom-test", "--dry-run"])
    assert "Stopped." in capsys.readouterr().out


def test_ctrl_c_during_the_model_download_says_nothing_changed(monkeypatch):
    from gesture_control import app, model

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(model, "ensure_model", interrupted)
    with pytest.raises(SystemExit) as exc:
        app.main(["--dry-run", "--no-preview"])
    assert "Download cancelled; nothing was changed" in str(exc.value.code)


# -- preview when the camera stalls ------------------------------------------------------

def _overlay(monkeypatch):
    import numpy as np

    from gesture_control import overlay as ov

    texts = []
    monkeypatch.setattr(ov, "_text", lambda img, s, *a, **k: texts.append(s))
    o = ov.Overlay.__new__(ov.Overlay)            # no window needed
    o.cfg = {"camera": {"width": 320, "height": 240}}
    o.name = "gesture-control"
    return o, texts, np


def test_stalled_preview_dims_the_last_frame_and_says_why(monkeypatch):
    o, texts, np = _overlay(monkeypatch)
    last = np.full((480, 640, 3), 200, np.uint8)
    monkeypatch.setattr("gesture_control.overlay.cv2.imshow", lambda *a: None)
    monkeypatch.setattr("gesture_control.overlay.cv2.waitKey", lambda *a: -1)
    o.show(last)
    img = o.stalled_image(3.2)
    assert img.shape == last.shape and img[0, 0, 0] < 100   # dimmed
    assert last[0, 0, 0] == 200                              # the frame itself untouched
    assert "No camera frames for 3 s" in texts
    assert any("another app" in t for t in texts)


def test_stalled_preview_before_any_frame_uses_the_configured_size(monkeypatch):
    o, texts, _ = _overlay(monkeypatch)
    assert o.stalled_image(1.0).shape == (240, 320, 3)


# -- scroll test mode ---------------------------------------------------------------------

def test_scroll_test_goes_down_up_right_left_and_back(capsys):
    from conftest import RecordingPointer

    from gesture_control.app import scroll_test

    ptr = RecordingPointer("absolute")
    scroll_test(dry_run=True, ptr=ptr, wait=lambda s: None)
    kinds = [(k, u) for k, u in ptr.log]
    assert kinds == ([("scroll", -24)] * 25 + [("scroll", 24)] * 25
                     + [("hscroll", 24)] * 25 + [("hscroll", -24)] * 25)
    assert ptr.scroll_total() == 0 and ptr.hscroll_total() == 0  # ends where it started
    assert "Scrolling right" in capsys.readouterr().out


def test_overlay_shows_the_scroll_axis(monkeypatch):
    o, texts, np = _overlay(monkeypatch)
    o.show_region = o.show_landmarks = False
    o.pinch_start, o.pinch_release = 0.28, 0.42
    from gesture_control.controller import Mode, Status

    for axis in ("vertical", "horizontal", "both"):
        o.draw(np.zeros((480, 640, 3), np.uint8), None,
               Status(mode=Mode.SCROLL, scroll_axis=axis), 30.0, 10.0)
    assert {"up/down", "sideways", "any way"} <= set(texts)
