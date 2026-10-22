"""The air keyboard: layout, typing (modifiers, Caps, repeat, Alt+Tab), the horns
toggle, the key-slip freeze, the Enter / Backspace gestures, config checks, and the
preview drawing. Keys are recorded by a dry-run virtual keyboard."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import DT, Rig, RecordingKeys, make_config

from gesture_control import app
from gesture_control.config import load_config
from gesture_control.controller import KEY_FREEZE_MAX_S, Mode
from gesture_control.features import FIST, HORNS, NONE, PALM, POINT, THREE, TWO
from gesture_control.keyboard import (
    CHARS,
    ROW_UNITS,
    Layout,
    Typer,
    gnome_layout,
    parse_spec,
    spec_label,
    specs_for_text,
)
from gesture_control.pointer import KEY_NAMES, VirtualKeys

HOLD = 0.8  # keyboard.toggle_hold_s


@pytest.fixture
def cfgdir(tmp_path):
    import shutil

    from gesture_control.config import DEFAULT_CONFIG_PATH
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    return tmp_path


def taps(keys: RecordingKeys) -> list[str]:
    """Keys pressed, in order (modifiers included)."""
    return [k for k, v in keys.log if v == 1]


def typed(keys: RecordingKeys) -> str:
    """What the recorded key presses type, US layout (Shift applied)."""
    out, held = "", set()
    for k, v in keys.log:
        if k in ("shift", "ctrl", "alt", "super"):
            (held.add if v else held.discard)(k)
        elif v == 1 and k in CHARS and not held & {"ctrl", "alt", "super"}:
            out += CHARS[k][1 if "shift" in held else 0]
        elif v == 1 and k == "backspace":
            out = out[:-1]
    return out


def center(layout: Layout, name: str) -> tuple[float, float]:
    x0, y0, x1, y1 = layout.keys[layout.find(name)].rect
    return ((x0 + x1) / 2, (y0 + y1) / 2)


# -- layout ------------------------------------------------------------------------

@pytest.fixture
def layout():
    return Layout.from_config(make_config())


def test_layout_rows_fill_the_region(layout):
    """Six rows (shortcuts + five), each from x_min to x_max with no gaps."""
    x0, x1, y0, y1 = layout.region
    assert layout.rows == 6
    for r in range(layout.rows):
        keys = [k for k in layout.keys if k.row == r]
        assert keys[0].rect[0] == pytest.approx(x0)
        assert keys[-1].rect[2] == pytest.approx(x1)
        for a, b in zip(keys, keys[1:]):
            assert a.rect[2] == pytest.approx(b.rect[0])


def test_every_point_inside_the_keyboard_hits_a_key(layout):
    x0, x1, y0, y1 = layout.region
    for x in np.linspace(x0, x1, 120, endpoint=False):
        for y in np.linspace(y0, y1, 40, endpoint=False):
            assert layout.hit(x, y) is not None


def test_key_centers_hit_their_own_key(layout):
    for i, k in enumerate(layout.keys):
        assert layout.hit((k.rect[0] + k.rect[2]) / 2, (k.rect[1] + k.rect[3]) / 2) == i


def test_outside_the_keyboard_is_no_key(layout):
    x0, x1, y0, y1 = layout.region
    assert layout.hit(x0 - 0.01, (y0 + y1) / 2) is None
    assert layout.hit((x0 + x1) / 2, y1 + 0.01) is None
    assert layout.hit(x1, y0) is None  # right edge is outside


def test_rows_are_15_units():
    from gesture_control.keyboard import ROWS
    for row in ROWS:
        assert sum(w for _, w in row) == ROW_UNITS


def test_shortcut_row_has_the_chosen_shortcuts(layout):
    top = [k.label for k in layout.keys if k.row == 0]
    assert top == ["Esc", "Tab", "Ctrl+Z", "Ctrl+X", "Ctrl+C", "Ctrl+V", "Ctrl+A",
                   "Ctrl+S", "Ctrl+F", "Alt+Tab", "Super"]
    bottom = [k.name for k in layout.keys if k.row == 5]
    assert bottom == ["ctrl", "alt", "home", "space", "end", "left", "up", "down", "right"]


def test_no_shortcut_row():
    lay = Layout((0.0, 1.0, 0.0, 0.5), [])
    assert lay.rows == 5 and lay.shortcut_rows == 0


@pytest.mark.parametrize("spec, parsed", [
    ("enter", ((), "enter")),
    ("ctrl+z", (("ctrl",), "z")),
    ("Ctrl+Shift+T", (("ctrl", "shift"), "t")),
    ("ctrl+=", (("ctrl",), "equal")),
    ("ctrl + /", (("ctrl",), "slash")),
    ("f5", ((), "f5")),
])
def test_parse_spec(spec, parsed):
    assert parse_spec(spec) == parsed


@pytest.mark.parametrize("spec", ["", "ctrl+", "cmd+z", "ctrl+ctrl+z", "enterr", 5, "+"])
def test_parse_spec_refuses(spec):
    with pytest.raises(ValueError):
        parse_spec(spec)


def test_spec_labels():
    assert spec_label("ctrl+z") == "Ctrl+Z"
    assert spec_label("alt+tab") == "Alt+Tab"
    assert spec_label("backspace") == "Bksp"


# -- typing ------------------------------------------------------------------------

@pytest.fixture
def kt():
    keys = RecordingKeys()
    return keys, Typer(keys, repeat_delay_s=0.5, repeat_rate=10, alt_tab_hold_s=1.5,
                       clock=lambda: 0.0)


def test_char_is_one_tap(kt):
    keys, typer = kt
    typer.press_spec("a", 0)
    assert keys.log == [("a", 1), ("a", 0)]
    assert typer.text == "a"


def test_shift_is_one_shot(kt, layout):
    keys, typer = kt
    typer.press(layout.keys[layout.find("shift")], 0)
    assert typer.latched == {"shift"}
    assert keys.log == []  # nothing sent until the next key
    typer.press(layout.keys[layout.find("a")], 0)
    typer.press(layout.keys[layout.find("b")], 0)
    assert typed(keys) == "Ab"
    assert typer.text == "Ab"
    assert not keys.any_down()


def test_tapping_a_modifier_again_cancels_it(kt, layout):
    keys, typer = kt
    shift = layout.keys[layout.find("shift")]
    typer.press(shift, 0)
    typer.press(shift, 0)
    typer.press(layout.keys[layout.find("a")], 0)
    assert typed(keys) == "a"


def test_caps_capitals_letters_only(kt, layout):
    keys, typer = kt
    typer.press(layout.keys[layout.find("caps")], 0)
    for name in ("h", "i", "1"):
        typer.press(layout.keys[layout.find(name)], 0)
    typer.press(layout.keys[layout.find("shift")], 0)
    typer.press(layout.keys[layout.find("x")], 0)  # Shift with Caps: small letter
    assert typed(keys) == "HI1x"


def test_latched_shift_combines_with_a_shortcut(kt, layout):
    keys, typer = kt
    typer.press(layout.keys[layout.find("shift")], 0)
    typer.press(layout.keys[layout.find("ctrl+z")], 0)
    assert keys.log == [("ctrl", 1), ("shift", 1), ("z", 1), ("z", 0),
                        ("shift", 0), ("ctrl", 0)]
    assert typer.text == ""  # shortcuts aren't echoed


def test_alt_tab_keeps_alt_until_timeout(kt, layout):
    keys, typer = kt
    at = layout.keys[layout.find("alt+tab")]
    typer.press(at, 0.0)
    typer.press(at, 1.0)
    assert keys.is_down("alt")
    assert [k for k, v in keys.log if k == "tab" and v == 1] == ["tab", "tab"]
    assert [k for k, v in keys.log if k == "alt"] == ["alt"]  # pressed once, still down
    typer.tick(2.4)
    assert keys.is_down("alt")      # 1.4 s after the last Alt+Tab
    typer.tick(2.5)
    assert not keys.is_down("alt")  # 1.5 s: picks the window


def test_another_key_ends_the_window_switcher_first(kt, layout):
    keys, typer = kt
    typer.press(layout.keys[layout.find("alt+tab")], 0.0)
    typer.press(layout.keys[layout.find("a")], 0.1)
    assert keys.log[-3:] == [("alt", 0), ("a", 1), ("a", 0)]


def test_repeat_after_delay_one_per_frame(kt):
    keys, typer = kt
    typer.press_spec("backspace", 0.0)
    t = 0.0
    while t < 1.0:
        t += DT
        typer.hold(t)
    n = [k for k, v in keys.log if v == 1].count("backspace")
    # 1 + repeats from 0.5 s at 10/s up to 1.0 s: 0.5, 0.6, ..., 1.0
    assert 5 <= n <= 7
    typer.lift()
    typer.hold(2.0)
    assert [k for k, v in keys.log if v == 1].count("backspace") == n


def test_slow_frames_dont_burst_repeats(kt):
    keys, typer = kt
    typer.press_spec("backspace", 0.0)
    typer.hold(3.0)  # one frame, 3 s later
    assert [k for k, v in keys.log if v == 1].count("backspace") == 2


def test_letters_dont_repeat(kt):
    keys, typer = kt
    typer.press_spec("a", 0.0)
    typer.hold(2.0)
    assert taps(keys) == ["a"]


def test_echo_backspace_and_enter(kt):
    keys, typer = kt
    for s in specs_for_text("hi there"):
        typer.press_spec(s, 0)
    typer.press_spec("backspace", 0)
    assert typer.text == "hi ther"
    typer.press_spec("enter", 0)
    assert typer.text == ""


def test_specs_for_text_types_the_text(kt):
    keys, typer = kt
    text = "Hello, World! (a+b)=c_1 ~/x?"
    for s in specs_for_text(text):
        typer.press_spec(s, 0)
    assert typed(keys) == text
    assert typer.text == text[-40:]


def test_release_all_lets_go_of_everything(kt, layout):
    keys, typer = kt
    typer.press(layout.keys[layout.find("alt+tab")], 0)
    typer.release_all()
    assert not keys.any_down()
    assert not typer.switching


def test_device_knows_every_key():
    k = VirtualKeys(dry_run=True, settle=0)
    assert set(k.KEYS) == set(KEY_NAMES)
    for name in CHARS:
        assert name in KEY_NAMES
    with pytest.raises(ValueError):
        k.press("nope")


# -- the controller ------------------------------------------------------------------

def kb_rig(**overrides) -> Rig:
    return Rig(make_config(**overrides), keys=True)


def open_keyboard(r: Rig):
    r.hold(3, pose=PALM)
    st = r.hold(int((HOLD + 0.2) / DT), pose=HORNS)
    assert st.mode == Mode.KEYBOARD
    return st


def type_key(r: Rig, name: str, close_frames: int = 4):
    """Point at the key, open the fingers (arms), close them (types)."""
    tip = center(r.ctl.layout, name)
    r.hold(3, pose=POINT, pinch_index=1.0, tip=tip)
    st = r.hold(close_frames, pose=POINT, pinch_index=0.1, tip=tip)
    r.hold(2, pose=POINT, pinch_index=1.0, tip=tip)
    return st


def test_horns_held_opens_the_keyboard():
    r = kb_rig()
    r.hold(3, pose=PALM)
    progress = [r.step(pose=HORNS).toggle_progress for _ in range(int(0.6 / DT))]
    assert r.ctl.mode != Mode.KEYBOARD
    assert progress[-1] > progress[3] > 0  # the bar fills
    r.hold(int(0.4 / DT), pose=HORNS)
    assert r.ctl.mode == Mode.KEYBOARD
    assert "keyboard open" in r.events


def test_brief_horns_doesnt_open_it():
    """A palm closing or a misread passes through horns for a few frames."""
    r = kb_rig()
    for _ in range(10):
        r.hold(4, pose=HORNS)
        r.hold(4, pose=PALM)
    assert r.ctl.mode != Mode.KEYBOARD


def test_holding_on_after_opening_doesnt_close_it():
    r = kb_rig()
    open_keyboard(r)
    r.hold(int(3 / DT), pose=HORNS)
    assert r.ctl.mode == Mode.KEYBOARD
    r.hold(3, pose=PALM)
    r.hold(int((HOLD + 0.2) / DT), pose=HORNS)
    assert r.ctl.mode == Mode.IDLE
    assert "keyboard closed" in r.events


def test_pinch_types_the_key_under_the_fingertip():
    r = kb_rig()
    open_keyboard(r)
    for name in ("h", "i"):
        type_key(r, name)
    assert typed(r.keys) == "hi"
    assert r.events.count("key H") == 1


def test_cursor_doesnt_move_while_typing():
    r = kb_rig()
    r.point(5)
    open_keyboard(r)
    n = len(r.ptr.log)
    r.move(20, dx=0.01, pose=POINT)
    type_key(r, "a")
    assert r.ptr.log[n:] == []
    assert not r.ptr.any_down()


def test_scroll_pose_doesnt_scroll_while_typing():
    r = kb_rig()
    open_keyboard(r)
    r.move(20, dy=-0.02, pose=TWO)
    assert r.ctl.mode == Mode.KEYBOARD
    assert r.ctl.scroller.pointer.scroll_total() == 0


def test_pinch_outside_the_keys_types_nothing():
    r = kb_rig()
    open_keyboard(r)
    r.hold(3, pose=POINT, pinch_index=1.0, tip=(0.5, 0.9))
    r.hold(4, pose=POINT, pinch_index=0.1)
    assert r.keys.log == []


def test_fingertip_slipping_while_pinching_keeps_the_key():
    """Bug guard: closing the pinch bends the index, so the fingertip slides onto the
    next key just as the click fires. Reproduced: hover g, close the fingers while the
    tip moves to h. Without the freeze, h would be typed."""
    r = kb_rig()
    open_keyboard(r)
    lay = r.ctl.layout
    g, h = center(lay, "g"), center(lay, "h")
    r.hold(10, pose=POINT, pinch_index=1.0, tip=g)   # aimed and settled on g
    assert lay.keys[r.last.kb_hover].name == "g"
    # Closing: distance falls below release, then start, while the tip moves to h.
    r.step(pose=POINT, pinch_index=0.38, tip=((g[0] + h[0]) / 2, g[1]))
    r.hold(6, pose=POINT, pinch_index=0.1, tip=h)
    assert typed(r.keys) == "g"
    # Without an armed closing pinch the hover follows the fingertip again.
    r.hold(10, pose=POINT, pinch_index=1.0, tip=h)
    assert lay.keys[r.last.kb_hover].name == "h"


def test_freeze_without_a_pinch_lets_go():
    """A thumb resting half-closed (below release, above start) doesn't freeze the
    selection for good."""
    r = kb_rig()
    open_keyboard(r)
    lay = r.ctl.layout
    r.hold(10, pose=POINT, pinch_index=1.0, tip=center(lay, "g"))
    r.hold(int((KEY_FREEZE_MAX_S + 0.3) / DT), pose=POINT, pinch_index=0.35,
           tip=center(lay, "k"))
    assert lay.keys[r.last.kb_hover].name == "k"
    assert r.keys.log == []


def test_holding_the_pinch_repeats_backspace_not_letters():
    r = kb_rig()
    open_keyboard(r)
    type_key(r, "a", close_frames=int(1.5 / DT))
    assert taps(r.keys) == ["a"]
    type_key(r, "backspace", close_frames=int(1.0 / DT))
    assert taps(r.keys).count("backspace") >= 4


def test_hand_lost_releases_alt_and_keeps_the_keyboard():
    r = kb_rig()
    open_keyboard(r)
    type_key(r, "alt+tab")
    assert r.keys.is_down("alt")
    r.no_hand(10)
    assert not r.keys.is_down("alt")
    assert r.ctl.mode == Mode.KEYBOARD
    assert "hand lost: released" in r.events


def test_camera_stall_releases_and_keeps_the_keyboard():
    r = kb_rig()
    open_keyboard(r)
    type_key(r, "alt+tab")
    r.ctl.stalled(r.t + 1)
    assert not r.keys.any_down()
    assert r.ctl.mode == Mode.KEYBOARD


def test_fist_pauses_and_closes_the_keyboard():
    r = kb_rig()
    open_keyboard(r)
    type_key(r, "shift")
    r.hold(int(1.3 / DT), pose=FIST)
    assert r.ctl.paused and r.ctl.mode == Mode.IDLE
    assert r.ctl.typer.latched == set()


def test_two_pointing_hands_dont_zoom_while_typing():
    r = Rig(make_config(zoom={"method": "keys"}), keys=True)
    open_keyboard(r)
    for i in range(20):
        r.step(pose=POINT, zoom=(True, 2.0 + 0.1 * i))
    assert r.ctl.mode == Mode.KEYBOARD
    assert "ctrl" not in taps(r.keys)


def test_keyboard_off_without_preview_or_when_disabled():
    for over in ({"preview": {"enabled": False}}, {"keyboard": {"enabled": False}}):
        r = kb_rig(**over)
        r.hold(3, pose=PALM)
        r.hold(int(2 / DT), pose=HORNS)
        assert r.ctl.mode != Mode.KEYBOARD
        assert r.last.toggle_progress == 0


def test_keyboard_needs_the_virtual_keyboard():
    r = Rig(make_config())  # no keys device
    r.hold(int(2 / DT), pose=HORNS)
    assert r.ctl.mode != Mode.KEYBOARD


def test_status_shows_hover_flash_and_echo():
    r = kb_rig()
    open_keyboard(r)
    lay = r.ctl.layout
    st = type_key(r, "q")
    assert st.kb_flash == lay.find("q")
    assert r.last.kb_text == "q"
    assert r.last.tip is not None
    r2 = kb_rig(keyboard={"echo": False})
    open_keyboard(r2)
    type_key(r2, "q")
    assert r2.last.kb_text == ""


def test_three_toggle_option():
    r = kb_rig(keyboard={"toggle": "three"})
    r.hold(3, pose=PALM)
    r.hold(int((HOLD + 0.2) / DT), pose=HORNS)
    assert r.ctl.mode != Mode.KEYBOARD
    r.hold(int((HOLD + 0.2) / DT), pose=THREE)
    assert r.ctl.mode == Mode.KEYBOARD


# -- Enter / Backspace gestures --------------------------------------------------------

def ring_pinch(r: Rig, frames: int = 4):
    r.hold(3, pose=PALM)
    r.hold(2, pose=NONE, up={"index", "middle", "pinky"}, pinch_ring=1.0)
    return r.hold(frames, pose=NONE, up={"index", "middle", "pinky"}, pinch_ring=0.1)


def pinky_pinch(r: Rig, frames: int = 4):
    r.hold(3, pose=PALM)
    r.hold(2, pose=THREE, pinch_pinky=1.0)
    return r.hold(frames, pose=THREE, pinch_pinky=0.1)


def test_thumb_to_ring_is_enter():
    r = kb_rig()
    ring_pinch(r)
    assert taps(r.keys) == ["enter"]
    r.hold(5, pose=PALM)
    ring_pinch(r)
    assert taps(r.keys) == ["enter", "enter"]


def test_thumb_to_pinky_is_backspace_and_repeats():
    r = kb_rig()
    pinky_pinch(r)
    assert taps(r.keys) == ["backspace"]
    pinky_pinch(r, frames=int(1.0 / DT))
    assert taps(r.keys).count("backspace") >= 5


def test_typing_gestures_need_arming():
    """Thumb already on the ring finger as the hand appears: nothing."""
    r = kb_rig()
    r.hold(10, pose=NONE, up={"index", "middle", "pinky"}, pinch_ring=0.1)
    assert r.keys.log == []


def test_thumb_on_curled_fingers_while_pointing_isnt_a_gesture():
    """Pointing: the thumb rests on the curled ring and pinky. Not Enter/Backspace."""
    r = kb_rig()
    r.hold(3, pose=POINT, pinch_ring=1.0, pinch_pinky=1.0)
    r.hold(10, pose=POINT, pinch_ring=0.1, pinch_pinky=0.15)
    assert r.keys.log == []


def test_typing_gestures_not_during_a_drag_or_scroll():
    r = kb_rig()
    r.point(3)
    r.left_pinch()
    assert r.ptr.is_down("left")
    r.hold(2, pose=POINT, up={"index", "middle", "pinky"}, pinch_index=0.1, pinch_ring=1.0)
    r.hold(4, pose=POINT, up={"index", "middle", "pinky"}, pinch_index=0.1, pinch_ring=0.1)
    assert r.keys.log == []
    r2 = kb_rig()
    r2.hold(10, pose=TWO)
    r2.hold(2, pose=TWO, up={"index", "middle", "pinky"}, pinch_ring=1.0)
    r2.hold(4, pose=TWO, up={"index", "middle", "pinky"}, pinch_ring=0.1)
    assert r2.keys.log == []


def test_typing_gestures_work_with_the_keyboard_open():
    r = kb_rig()
    open_keyboard(r)
    ring_pinch(r)
    assert taps(r.keys) == ["enter"]


def test_gestures_rebound_and_off():
    r = kb_rig(keyboard={"gestures": {"ring_pinch": "ctrl+z", "pinky_pinch": "none"}})
    ring_pinch(r)
    pinky_pinch(r)
    assert taps(r.keys) == ["ctrl", "z"]


def test_pinky_pinch_doesnt_open_a_three_toggle():
    """With toggle: three, holding Backspace (thumb on pinky = the three pose) must
    not open the keyboard."""
    r = kb_rig(keyboard={"toggle": "three"})
    pinky_pinch(r, frames=int(2 / DT))
    assert r.ctl.mode != Mode.KEYBOARD


# -- config ------------------------------------------------------------------------------

@pytest.mark.parametrize("name, value, msg", [
    ("keyboard.toggle", "fist", "keyboard.toggle"),
    ("keyboard.shortcuts", ["ctrl+zz"], "keyboard.shortcuts"),
    ("keyboard.shortcuts", "esc", "keyboard.shortcuts"),
    ("keyboard.shortcuts", ["esc"] * 17, "up to 16"),
    ("keyboard.gestures.ring_pinch", "entr", "keyboard.gestures.ring_pinch"),
    ("keyboard.region.x_max", 0.2, "keyboard.region"),
    ("keyboard.region.y_max", 0.15, "keyboard.region"),
    ("keyboard.repeat_rate", 0, "keyboard.repeat_rate"),
    ("keyboard.echo", "true", "keyboard.echo"),
])
def test_bad_keyboard_settings_are_refused(cfgdir, name, value, msg):
    with pytest.raises(ValueError, match=msg.replace(".", r"\.")):
        load_config(cfgdir / "gestures.yaml", {name: value})


def test_keyboard_settings_from_the_command_line(cfgdir):
    args = app.parse_args(["--keyboard-shortcuts", "[esc, ctrl+shift+t]",
                           "--keyboard-gestures-pinky-pinch", "none"])
    cfg = load_config(cfgdir / "gestures.yaml", args.overrides)
    assert cfg["keyboard"]["shortcuts"] == ["esc", "ctrl+shift+t"]
    assert cfg["keyboard"]["gestures"]["pinky_pinch"] == "none"


# -- app -------------------------------------------------------------------------------------

def test_keys_we_typed_into_the_preview_are_ignored(monkeypatch):
    r = kb_rig()
    open_keyboard(r)
    clock = {"t": 50.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])
    r.ctl.typer.clock = lambda: clock["t"]
    type_key(r, "q")
    said = [False]
    assert app._preview_key(ord("q"), r.ctl, said) == 255  # ours: not a quit
    assert said[0]
    assert r.step(pose=POINT).kb_warn
    clock["t"] += 2.0
    assert app._preview_key(ord("q"), r.ctl, said) == ord("q")  # the user's own q
    assert app._preview_key(255, r.ctl, said) == 255


def test_keyboard_test_types_the_sentence(capsys, monkeypatch):
    seen = []
    keys = RecordingKeys()
    monkeypatch.setattr(app, "_make_keys", lambda dry_run, settle=0.3: keys)
    app.keyboard_test(True, wait=lambda s: seen.append(s))
    assert typed(keys) == app.KEYBOARD_TEST_RESULT
    assert seen[:3] == [1, 1, 1]  # the countdown
    assert app.KEYBOARD_TEST_RESULT in capsys.readouterr().out


def test_keyboard_test_from_the_command_line(monkeypatch, cfgdir):
    called = {}
    monkeypatch.setattr(app, "keyboard_test", lambda dry_run: called.setdefault("d", dry_run))
    app.main(["--config", str(cfgdir / "gestures.yaml"), "--keyboard-test", "--dry-run"])
    assert called["d"] is True


@pytest.mark.parametrize("out, layout", [
    ("[('xkb', 'us')]\n", "us"),
    ("[('xkb', 'de'), ('xkb', 'us')]\n", "de"),
    ("[('ibus', 'anthy'), ('xkb', 'us+intl')]\n", "us+intl"),
    ("@a(ss) []\n", None),
    ("garbage", None),
])
def test_gnome_layout(out, layout):
    assert gnome_layout(run=lambda cmd: out) == layout


# -- preview ----------------------------------------------------------------------------------

@pytest.fixture
def overlay(monkeypatch):
    from gesture_control import overlay as ov

    monkeypatch.setattr(ov.cv2, "namedWindow", lambda *a, **k: None)  # no real window
    return ov.Overlay(make_config())


def test_preview_draws_the_keyboard_and_hovered_key(overlay):
    r = kb_rig()
    open_keyboard(r)
    lay = r.ctl.layout
    st = r.hold(10, pose=POINT, pinch_index=1.0, tip=center(lay, "k"))
    frame = np.zeros((480, 640, 3), np.uint8)
    overlay.draw(frame, None, st, 30.0, 40.0)
    x0, y0, x1, y1 = lay.keys[lay.find("k")].rect
    px = frame[int(y0 * 480) + 4, int(x0 * 640) + 4]
    from gesture_control.overlay import YELLOW
    assert tuple(px) == YELLOW  # the hovered key is filled


def test_preview_draws_every_mode_and_band(overlay):
    """No drawing errors for: normal panel with the new meters, the toggle bar, the
    warning, keyboard mode with modifiers and Caps."""
    r = kb_rig()
    frame = np.zeros((480, 640, 3), np.uint8)
    overlay.draw(frame, None, r.step(pose=PALM), 30.0, 40.0)
    st = r.hold(10, pose=HORNS)
    assert st.toggle_progress > 0
    overlay.draw(frame, None, st, 30.0, 40.0)
    open_keyboard(r)
    for name in ("caps", "alt+tab", "shift"):  # a key typed clears a latched Shift
        type_key(r, name)
    st = r.step(pose=POINT)
    assert st.kb_latched == ("shift",) and st.kb_caps and st.kb_switching
    overlay.draw(frame, None, st, 30.0, 40.0)
    r.ctl.typed_into_preview()
    overlay.draw(frame, None, r.step(pose=POINT), 30.0, 40.0)
