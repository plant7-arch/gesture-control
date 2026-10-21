"""Virtual pointer and touchpad logic that doesn't need /dev/uinput (dry-run)."""

import pytest
from conftest import QuietTouchpad

from gesture_control import touchpad as tpm
from gesture_control.config import DEFAULTS
from gesture_control.pointer import HI_RES_PER_NOTCH, VirtualKeys, VirtualPointer

# -- pointer ------------------------------------------------------------------------


def test_buttons_are_never_pressed_or_released_twice(capsys):
    p = VirtualPointer(dry_run=True, settle=0)
    p.press("left")
    p.press("left")
    assert p.is_down("left") and p.any_down()
    p.release("left")
    p.release("left")
    assert not p.any_down()
    assert capsys.readouterr().out.splitlines() == ["[pointer] left down", "[pointer] left up"]


def test_release_all_lets_go_of_everything():
    p = VirtualPointer(dry_run=True, settle=0)
    p.press("left")
    p.press("right")
    p.release_all()
    assert not p.any_down()


def test_close_releases_a_held_button(capsys):
    p = VirtualPointer(dry_run=True, settle=0)
    p.press("left")
    p.close()
    assert capsys.readouterr().out.splitlines()[-1] == "[pointer] left up"


def test_smooth_scroll_adds_up_to_whole_notches(capsys):
    """Hi-res units accumulate; the legacy wheel gets one notch per 120 units, so apps
    without hi-res scrolling still scroll the same total distance."""
    p = VirtualPointer(dry_run=True, settle=0)
    for _ in range(5):
        p.scroll(HI_RES_PER_NOTCH // 2)
    p.scroll(-HI_RES_PER_NOTCH * 3)
    notches = [int(line.split("notches ")[1].rstrip(")"))
               for line in capsys.readouterr().out.splitlines()]
    assert notches == [0, 1, 0, 1, 0, -2]
    # 300 - 360 = -60 units: no whole notch is lost, -60 waits for the next scroll.
    assert p._wheel_acc == -60


def test_sideways_scroll_adds_up_to_whole_notches_on_its_own(capsys):
    """The sideways wheel keeps its own count: mixing both axes loses nothing."""
    p = VirtualPointer(dry_run=True, settle=0)
    for _ in range(3):
        p.scroll_h(HI_RES_PER_NOTCH // 2)
        p.scroll(HI_RES_PER_NOTCH // 3)
    lines = capsys.readouterr().out.splitlines()
    h = [int(x.split("notches ")[1].rstrip(")")) for x in lines if x.startswith("[pointer] hscroll")]
    v = [int(x.split("notches ")[1].rstrip(")")) for x in lines if x.startswith("[pointer] scroll")]
    assert h == [0, 1, 0] and v == [0, 0, 1]
    assert p._hwheel_acc == 60 and p._wheel_acc == 0
    p.release_all()
    assert p._hwheel_acc == p._wheel_acc == 0


class _FakeUI:
    def __init__(self):
        self.events = []

    def write(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append("syn")


def test_wheel_events_use_the_right_kernel_codes():
    """Up/down: REL_WHEEL(_HI_RES); sideways: REL_HWHEEL(_HI_RES), positive = right."""
    from evdev import ecodes as e

    p = VirtualPointer(dry_run=True, settle=0)
    p.dry_run, p._e, p._hi_res, p._ui = False, e, True, _FakeUI()
    p.scroll_h(HI_RES_PER_NOTCH)
    p.scroll(-HI_RES_PER_NOTCH // 2)
    assert p._ui.events == [
        (e.EV_REL, e.REL_HWHEEL_HI_RES, 120), (e.EV_REL, e.REL_HWHEEL, 1), "syn",
        (e.EV_REL, e.REL_WHEEL_HI_RES, -60), "syn",
    ]


def test_absolute_positions_are_clamped_and_not_repeated(capsys):
    p = VirtualPointer(dry_run=True, settle=0)
    p.move_abs(1.5, -0.2)
    p.move_abs(1.0, 0.0)   # same device position after clamping: not sent again
    assert capsys.readouterr().out.count("[pointer] abs") == 1


def test_keys_tap_presses_and_releases(capsys):
    k = VirtualKeys(dry_run=True, settle=0)
    k.press("ctrl")
    k.tap("equal")
    k.close()
    assert capsys.readouterr().out.splitlines() == [
        "[keys] ctrl down", "[keys] equal down", "[keys] equal up", "[keys] ctrl up"]
    assert not k.is_down("ctrl")


# -- touchpad -------------------------------------------------------------------------

@pytest.fixture
def tp(fake_clock):
    return QuietTouchpad(DEFAULTS["zoom"]["touchpad"])


def test_finger_separation_is_kept_inside_the_safe_area(tp):
    assert tp.begin(1.0) == pytest.approx(tp.min_sep)
    tp.move(1000.0)
    assert tp.separation == pytest.approx(tp.max_sep)
    # The fingers sit far enough apart vertically that libinput can't call it a scroll.
    (_, ay), (_, by) = tp.finger_mm(tp.min_sep)
    assert abs(ay - by) >= tpm.MIN_DY_MM - 1e-9


def test_fingers_stay_inside_libinput_palm_and_thumb_zones(tp):
    w, h = tp.width_mm, tp.height_mm
    for sep in (tp.min_sep, tp.max_sep):
        for x, y in tp.finger_mm(sep):
            assert 0.08 * w < x < 0.92 * w
            assert 0.05 * h < y < 0.85 * h


def test_quick_lift_is_delayed_so_it_is_not_a_tap(tp, fake_clock):
    """A two-finger touch shorter than the tap timeout would be a right click."""
    tp.begin(45)
    fake_clock.t += 0.1
    tp.end()
    assert tp.is_down and tp.lift_pending
    tp.tick()
    assert tp.is_down                     # still too early
    fake_clock.t += tpm.MIN_CONTACT_S
    tp.tick()
    assert not tp.is_down
    assert [e[0] for e in tp.events] == ["down", "up"]


def test_lift_after_long_contact_is_immediate(tp, fake_clock):
    tp.begin(45)
    fake_clock.t += 1.0
    tp.end()
    assert not tp.is_down


def test_new_pinch_during_pending_lift_continues_the_same_contact(tp, fake_clock):
    tp.begin(45)
    tp.move(60)
    fake_clock.t += 0.05
    tp.end()                              # lift scheduled, fingers still down
    assert tp.begin(45) == pytest.approx(60)   # continues from where the fingers are
    assert not tp.lift_pending
    assert [e[0] for e in tp.events] == ["down", "move"]


def test_moves_are_ignored_while_lifting(tp, fake_clock):
    tp.begin(45)
    tp.end()
    tp.move(70)
    assert tp.separation == pytest.approx(45)


def test_close_always_lifts(tp, fake_clock, monkeypatch):
    monkeypatch.setattr(tpm.time, "sleep", lambda s: None)
    tp.begin(45)
    tp.close()
    assert not tp.is_down


@pytest.mark.parametrize("mode", ["absolute", "relative"])
def test_pointer_device_declares_the_sideways_wheel(monkeypatch, mode):
    """Without REL_HWHEEL in its capabilities the kernel would drop sideways scrolls."""
    from evdev import ecodes as e

    from gesture_control import pointer as pm

    seen = {}
    monkeypatch.setattr(pm, "make_uinput", lambda caps, **kw: seen.update(caps) or _FakeUI())
    VirtualPointer(mode=mode, dry_run=False, settle=0)
    rel = seen[e.EV_REL]
    assert {e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES} <= set(rel)
