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
    """Mouse wheel: 5 notches each way, ending where it started."""
    from conftest import RecordingPointer

    from gesture_control.app import scroll_test
    from gesture_control.scrolling import Scroller

    ptr = RecordingPointer("absolute")
    scroll_test(Scroller(ptr), wait=lambda s: None, clock=lambda: 0.0)
    kinds = [(k, u) for k, u in ptr.log]
    assert kinds == ([("scroll", -24)] * 25 + [("scroll", 24)] * 25
                     + [("hscroll", 24)] * 25 + [("hscroll", -24)] * 25)
    assert ptr.scroll_total() == 0 and ptr.hscroll_total() == 0  # ends where it started
    out = capsys.readouterr().out
    assert "Scroll test (wheel)" in out and "Scrolling right" in out


def test_scroll_test_with_the_touchpad_swipes_each_way_and_lifts_in_between():
    from conftest import RecordingPointer

    from gesture_control.app import scroll_test
    from gesture_control.scrolling import PadScroller, Scroller

    clock = {"t": 0.0}

    def wait(s):
        clock["t"] += s

    pad = PadScroller(5.0, None, dry_run=True, settle=0)
    ptr = RecordingPointer("absolute")
    scroll_test(Scroller(ptr, pad), wait=wait, clock=lambda: clock["t"])
    kinds = [e[0] for e in pad.events]
    assert kinds.count("down") == 4 and kinds.count("up") == 4   # one swipe per direction
    assert ptr.log == []                                         # nothing on the wheel
    downs = [e for e in pad.events if e[0] == "down"]
    assert all(e[1:] == pad.centre for e in downs)


def test_scroll_method_can_be_chosen_on_the_command_line(monkeypatch):
    from gesture_control import app

    seen = {}
    monkeypatch.setattr(app, "_make_scroller",
                        lambda cfg, *a, **k: seen.update(method=cfg["scroll"]["method"]))
    monkeypatch.setattr(app, "scroll_test", lambda *a, **k: None)
    app.main(["--scroll-test", "--dry-run", "--scroll-method", "wheel"])
    assert seen["method"] == "wheel"


def test_overlay_shows_the_scroll_axis(monkeypatch):
    o, texts, np = _overlay(monkeypatch)
    o.show_region = o.show_landmarks = False
    o.pinch_start, o.pinch_release = 0.28, 0.42
    from gesture_control.controller import Mode, Status

    for axis in ("vertical", "horizontal", "both"):
        o.draw(np.zeros((480, 640, 3), np.uint8), None,
               Status(mode=Mode.SCROLL, scroll_axis=axis), 30.0, 10.0)
    assert {"up/down", "sideways", "any way"} <= set(texts)


def test_stalled_preview_with_the_phone_asks_about_the_phone(monkeypatch):
    """v0.18.1: the stall band said 'Is another app using the camera?' for the phone
    too. With the phone, it asks whether the phone is still connected."""
    from gesture_control import app

    o, texts, np = _overlay(monkeypatch)
    o.stall_hint = app.PHONE_STALL_HINT
    o.stalled_image(7.0)
    assert any(t.startswith("Is the phone still connected") for t in texts)
    assert not any("another app" in t for t in texts)


def test_stall_hints_fit_a_640_pixel_window():
    import cv2

    from gesture_control import app
    from gesture_control.overlay import FONT

    for hint in (app.PHONE_STALL_HINT, app.CAMERA_STALL_HINT):
        (w, _), _ = cv2.getTextSize(hint, FONT, 0.45, 1)
        assert 20 + w <= 640
