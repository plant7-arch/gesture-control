"""Scrolling as a two-finger swipe on a virtual touchpad (scroll.method: touchpad),
and the switch between it and the mouse wheel. The pad runs in dry-run mode and
records the finger events it would send."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from conftest import DT, Rig

from gesture_control import scrolling as sc
from gesture_control.features import PALM, TWO
from gesture_control.scrolling import PadScroller, Scroller
from gesture_control.touchpad import TouchpadSettings

NOTCH = 120


def _settings(natural=True, usable=True, two_finger=True):
    return SimpleNamespace(natural=natural, scroll_usable=usable and two_finger)


def _pad(mm=5.0, **kw) -> PadScroller:
    return PadScroller(mm, _settings(**kw), dry_run=True, settle=0)


def _moves(pad):
    """Finger movement (dx, dy) in mm for each move event, from where they were."""
    out, prev = [], None
    for ev in pad.events:
        if ev[0] in ("down", "move"):
            if ev[0] == "move":
                out.append((ev[1] - prev[0], ev[2] - prev[1]))
            prev = (ev[1], ev[2])
    return out


def _total(pad):
    m = _moves(pad)
    return sum(d[0] for d in m), sum(d[1] for d in m)


def _run(pad, frames, t0=0.0, each=None):
    t = t0
    for _ in range(frames):
        if each:
            each(pad)
        t += DT
        pad.flush(t)
    return t


# -- direction and distance ------------------------------------------------------------

@pytest.mark.parametrize("natural, ux, uy, fx, fy", [
    (True, 0, -NOTCH, 0, -5),     # wheel down (content up) = fingers up, with natural
    (True, 0, NOTCH, 0, 5),       # wheel up = fingers down
    (True, NOTCH, 0, -5, 0),      # wheel right (content left) = fingers left
    (True, -NOTCH, 0, 5, 0),      # wheel left = fingers right
    (False, 0, -NOTCH, 0, 5),     # traditional touchpad scrolling: the other way
    (False, NOTCH, 0, 5, 0),
])
def test_fingers_move_the_way_that_gives_the_scroll_asked_for(natural, ux, uy, fx, fy):
    """GNOME's touchpad natural-scroll setting applies to the pad, so the fingers move
    whichever way makes the content go where the hand asked."""
    pad = _pad(natural=natural)
    pad.scroll(ux, uy)
    _run(pad, 3)
    assert _total(pad) == pytest.approx((fx, fy))


def test_mm_per_notch_sets_the_finger_travel():
    pad = _pad(mm=8.0)
    pad.scroll(0, 3 * NOTCH)
    _run(pad, 3)
    assert _total(pad) == pytest.approx((0, 24))


def test_fingers_land_first_and_move_a_frame_later():
    """Landing and moving in one go would look like a sensor jump to libinput."""
    pad = _pad()
    pad.scroll(0, NOTCH)
    pad.flush(1.0)
    assert pad.events == [("down", 500.0, 500.0)]
    pad.flush(1.0 + sc.MIN_LAND_S / 2)            # too soon
    assert len(pad.events) == 1
    pad.flush(1.0 + DT)
    assert pad.events[-1][0] == "move"


def test_one_finger_event_per_frame_carries_everything_scrolled_in_it():
    pad = _pad()
    pad.scroll(0, 10)
    pad.flush(0.0)                                # lands
    for _ in range(5):
        pad.scroll(0, 24)
    pad.flush(DT)
    assert [e[0] for e in pad.events] == ["down", "move"]
    assert _total(pad)[1] == pytest.approx((10 + 5 * 24) * 5 / NOTCH)


def test_fingers_never_move_faster_than_libinput_accepts():
    """libinput drops sudden jumps (about 20 mm in 12 ms) as sensor glitches, so a big
    scroll is spread over several frames instead of lost."""
    pad = _pad()
    pad.scroll(0, 40 * NOTCH)                     # 200 mm at once
    _run(pad, 12)
    steps = [math.hypot(*m) for m in _moves(pad)]
    assert max(steps) <= sc.MAX_SPEED_MM_S * DT + 1e-6
    assert len(steps) > 3
    assert _total(pad)[1] == pytest.approx(200.0)


# -- lifting ------------------------------------------------------------------------------

def test_fingers_lift_only_after_holding_still():
    """Apps add their own momentum to a swipe that lifts while moving."""
    pad = _pad()
    pad.scroll(0, NOTCH)
    t = _run(pad, 2)
    assert pad.is_down
    t = _run(pad, int(sc.STILL_BEFORE_LIFT_S / DT) - 1, t)
    assert pad.is_down
    _run(pad, 2, t)
    assert not pad.is_down and pad.events[-1] == ("up",)


def test_steady_scrolling_keeps_the_fingers_down():
    pad = _pad()
    _run(pad, 60, each=lambda p: p.scroll(0, 12))
    assert [e[0] for e in pad.events].count("down") == 1 and pad.is_down


def test_out_of_room_the_fingers_stop_lift_and_land_in_the_middle():
    """A long scroll: hold still, lift, land in the middle, carry on. Nothing is lost."""
    pad = _pad()
    stamped = []                                   # (time, event) as they are sent
    t = 0.0
    for i in range(230):
        if i < 200:
            pad.scroll(0, 3 * NOTCH)               # 15 mm per frame, 3 m in all
        t += DT
        n = len(pad.events)
        pad.flush(t)
        stamped += [(t, e) for e in pad.events[n:]]
    kinds = [e[0] for _, e in stamped]
    assert kinds.count("down") >= 6                # 1 m pad: several re-grips
    x0, x1, y0, y1 = pad.limits
    assert all(y0 <= e[2] <= y1 for _, e in stamped if e[0] != "up")
    assert _total(pad)[1] == pytest.approx(200 * 15.0)
    # every lift mid-scroll comes after the fingers held still for REGRIP_STILL_S
    last_move = None
    for tt, e in stamped:
        if e[0] == "move":
            last_move = tt
        elif e[0] == "up" and last_move is not None:
            assert tt - last_move >= sc.REGRIP_STILL_S - 1e-9


def test_release_lifts_at_once_and_forgets_the_rest():
    pad = _pad()
    pad.scroll(0, 10 * NOTCH)
    _run(pad, 2)
    pad.release()
    assert not pad.is_down
    n = len(pad.events)
    _run(pad, 10)
    assert len(pad.events) == n


# -- touchpad or wheel ---------------------------------------------------------------------

class _Wheel:
    def __init__(self):
        self.log = []

    def scroll(self, u):
        self.log.append(("v", u))

    def scroll_h(self, u):
        self.log.append(("h", u))


def test_without_a_pad_scrolling_is_the_mouse_wheel():
    w = _Wheel()
    s = Scroller(w)
    s.scroll(24, -48)
    s.flush(0.0)
    assert w.log == [("v", -48), ("h", 24)] and s.method == "wheel"


def test_pad_is_used_while_gnome_allows_it_and_the_wheel_otherwise():
    w, settings = _Wheel(), _settings()
    pad = PadScroller(5.0, settings, dry_run=True, settle=0)
    s = Scroller(w, pad, settings)
    s.scroll(0, NOTCH)
    s.flush(0.0)
    s.flush(DT)
    assert s.method == "touchpad" and pad.is_down and w.log == []
    settings.scroll_usable = False                 # touchpad turned off in Settings
    s.scroll(0, NOTCH)
    assert s.method == "wheel" and w.log == [("v", NOTCH)]
    assert not pad.is_down                         # the pad let go


# -- through the gesture controller -----------------------------------------------------

def _rig_with_pad(**kw):
    r = Rig()
    pad = _pad(**kw)
    r.ctl.scroller = Scroller(r.ptr, pad, pad.settings)
    return r, pad


def test_peace_sign_drag_moves_the_fingers_with_the_hand():
    """Natural scrolling everywhere: hand up = content up = fingers up."""
    r, pad = _rig_with_pad()
    r.hold(12, pose=TWO)
    r.move(15, dy=-0.01, pose=TWO)                 # hand up 0.15 frame heights
    r.hold(10, pose=TWO)
    dx, dy = _total(pad)
    # 0.15 heights x 25 notches x 5 mm = 18.75 mm (less the deadzone's last step)
    assert dx == 0 and -18.75 <= dy <= -18.75 * 0.8
    assert r.ptr.of("scroll") == []                # nothing on the wheel


def test_sideways_drag_moves_the_fingers_sideways():
    r, pad = _rig_with_pad()
    r.hold(12, pose=TWO)
    r.move(15, dx=0.0075, pose=TWO)                # hand right 0.15 frame heights
    r.hold(10, pose=TWO)
    dx, dy = _total(pad)
    assert dy == 0 and 18.75 * 0.8 <= dx <= 18.75


def test_fling_keeps_the_fingers_gliding_then_lifts_them_when_still():
    r, pad = _rig_with_pad()
    r.hold(12, pose=TWO)
    r.move(6, dy=-0.05, pose=TWO)
    n = len(_moves(pad))
    r.step(pose=PALM)
    r.hold(10, pose=PALM)
    assert len(_moves(pad)) > n + 8 and pad.is_down   # still gliding, fingers down
    r.hold(300, pose=PALM)
    assert not pad.is_down and not r.ctl.gliding


def test_catching_the_glide_stops_the_fingers():
    r, pad = _rig_with_pad()
    r.hold(12, pose=TWO)
    r.move(6, dy=-0.05, pose=TWO)
    r.step(pose=PALM)
    r.hold(10, pose=PALM)
    r.hold(2, pose=TWO)                            # catch
    n = len(_moves(pad))
    r.hold(5, pose=TWO)
    assert len(_moves(pad)) == n


def test_camera_stall_lifts_the_scroll_fingers():
    r, pad = _rig_with_pad()
    r.hold(12, pose=TWO)
    r.move(6, dy=-0.05, pose=TWO)
    assert pad.is_down
    r.ctl.stalled(r.t + 1.0)
    assert not pad.is_down


# -- GNOME settings ---------------------------------------------------------------------

@pytest.mark.parametrize("out, default, expected", [
    ("true\n", False, True), ("false\n", True, False), ("", True, True),
    ("No such key\n", False, False),
])
def test_touchpad_bool_settings_are_read(monkeypatch, out, default, expected):
    from gesture_control import touchpad as tpm

    monkeypatch.setattr(tpm.shutil, "which", lambda _: "/usr/bin/gsettings")
    monkeypatch.setattr(tpm.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=out, returncode=0))
    assert TouchpadSettings.read_bool("natural-scroll", default) is expected


@pytest.mark.parametrize("state, two_finger, usable, words", [
    ("enabled", True, True, ""),
    ("enabled", False, False, "two-finger scrolling is turned off"),
    ("disabled", True, False, "turned off in Settings"),
    ("disabled-on-external-mouse", True, False, "virtual pointer counts as a mouse"),
])
def test_scroll_falls_back_to_the_wheel_and_says_why(state, two_finger, usable, words):
    s = TouchpadSettings.__new__(TouchpadSettings)
    s.state, s.two_finger, s.natural = state, two_finger, True
    assert s.scroll_usable is usable
    assert (words in s.scroll_message()) if words else s.scroll_message() == ""


# -- the real device ---------------------------------------------------------------------

class _FakeUI:
    def __init__(self):
        self.events = []

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append("syn")

    def close(self):
        self.events.append("closed")


def test_device_is_a_large_touchpad_and_sends_two_fingers(monkeypatch):
    from evdev import ecodes as e

    seen = {}

    def make(caps, **kw):
        seen.update(caps=caps, **kw)
        return _FakeUI()

    monkeypatch.setattr(sc, "make_uinput", make)
    pad = PadScroller(5.0, _settings(), dry_run=False, settle=0)
    keys = seen["caps"][e.EV_KEY]
    assert e.BTN_TOOL_DOUBLETAP in keys and e.BTN_TOUCH in keys
    absinfo = dict(seen["caps"][e.EV_ABS])[e.ABS_MT_POSITION_X]
    assert absinfo.max / absinfo.resolution == sc.PAD_MM
    assert seen["name"] == "gesture-control scroll pad" and seen["bustype"] == e.BUS_USB
    pad.close()
    assert pad._ui is None


def test_device_events_land_move_and_lift(monkeypatch):
    from evdev import ecodes as e

    ui = _FakeUI()
    monkeypatch.setattr(sc, "make_uinput", lambda caps, **kw: ui)
    pad = PadScroller(5.0, _settings(), dry_run=False, settle=0)
    pad.scroll(0, NOTCH)
    pad.flush(0.0)
    pad.flush(DT)
    pad.release()
    ids = [v for t, c, v in (x for x in ui.events if x != "syn") if c == e.ABS_MT_TRACKING_ID]
    assert ids[:2] == [1, 2] and ids[-2:] == [-1, -1]       # two fingers down, then up
    touch = [v for t, c, v in (x for x in ui.events if x != "syn") if c == e.BTN_TOUCH]
    assert touch == [1, 0]
    ys = [v for t, c, v in (x for x in ui.events if x != "syn") if c == e.ABS_MT_POSITION_Y]
    assert ys[0] == 500 * sc.RES and ys[-1] == (500 + 5) * sc.RES  # moved down 5 mm
    assert ui.events.count("syn") == 3                       # land, move, lift
