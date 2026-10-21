"""Sideways scrolling: everything vertical scrolling does (drag, direction, deadzone,
settle, exact distance, fling, boost, catch, stack, misread protection), left/right,
plus the axis lock that keeps each swipe on one axis.

Hand movement is given in frame heights on both axes, the unit scrolling uses: the
fake camera is 640x480, so x moves 0.75 frame widths per frame height."""

from __future__ import annotations

import pytest
from conftest import DT, Rig, make_config

from gesture_control.controller import Status
from gesture_control.features import NONE, PALM, POINT, TWO

ASPECT = 480 / 640  # frame widths per frame height


def _start(r: Rig) -> None:
    r.hold(12, pose=TWO)        # debounce + settle_ms (200)
    r.ptr.log.clear()


def _move(r: Rig, n: int, right: float = 0.0, up: float = 0.0, pose=TWO):
    """n frames, the hand moving right / up by these many frame heights per frame."""
    return r.move(n, dx=right * ASPECT, dy=-up, pose=pose)


def _sideways_swipe_and_lift(r: Rig, per_frame: float, frames: int = 6,
                             stop_first: bool = False) -> list:
    _start(r)
    _move(r, frames, right=per_frame)
    if stop_first:
        r.hold(10, pose=TWO)
    n = len(r.ptr.of("hscroll"))
    _move(r, 1, right=per_frame, pose=PALM)
    r.hold(20, pose=PALM)
    return r.ptr.of("hscroll")[n:]


def _glide_total(r: Rig, axis: str, frames: int = 200) -> int:
    n = len(r.ptr.of(axis))
    r.hold(frames, pose=PALM)
    return sum(u for _, u in r.ptr.of(axis)[n:])


# -- drag ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction, sign", [("natural", -1), ("traditional", 1)])
def test_sideways_direction(direction, sign):
    """natural (phone): hand right -> content right -> wheel left (negative)."""
    r = Rig(make_config(scroll={"direction": direction}))
    _start(r)
    _move(r, 15, right=0.01)
    r.hold(10, pose=TWO)
    total = r.ptr.hscroll_total()
    assert total * sign > 0
    # 0.15 frame heights x 25 notches per height x 120 = 450 units, as for up/down
    assert 0.8 * 450 <= abs(total) <= 450
    assert r.ptr.of("scroll") == []


def test_same_hand_distance_scrolls_the_same_either_way():
    def total(right, up):
        r = Rig()
        _start(r)
        _move(r, 20, right=right, up=up)
        r.hold(15, pose=TWO)
        return abs(r.ptr.hscroll_total()), abs(r.ptr.scroll_total())

    side, _ = total(0.008, 0.0)
    _, vert = total(0.0, 0.008)
    assert side == pytest.approx(vert, abs=2)


def test_sideways_uses_the_cameras_real_frame_shape():
    """With a 1280x720 camera, frame heights are 0.5625 frame widths."""
    r = Rig()
    r.ctl.set_frame_size(1280, 720)
    _start(r)
    r.move(20, dx=0.008 * 720 / 1280, pose=TWO)   # 0.16 frame heights to the right
    r.hold(15, pose=TWO)
    # within the last step the deadzone holds back (0.0015 frame heights = 4.5 units)
    assert abs(r.ptr.hscroll_total()) == pytest.approx(0.16 * 25 * 120, abs=5.5)


def test_still_peace_sign_does_not_scroll_sideways(rig):
    _start(rig)
    for i in range(30):   # tracking jitter on both axes
        j = 0.0004 if i % 2 else -0.0004
        rig.step(pose=TWO, anchor=(0.5 + j, 0.5 - j))
    assert rig.ptr.of("hscroll") == [] and rig.ptr.of("scroll") == []


def test_sideways_movement_during_settle_is_ignored(rig):
    rig.hold(3, pose=TWO)                      # just entered SCROLL
    _move(rig, 4, right=0.02)                  # hand still arriving
    assert rig.ptr.of("hscroll") == []


@pytest.mark.parametrize("per_frame", [0.00153, 0.00175, 0.0021])
def test_slow_sideways_scroll_moves_exactly_as_far_as_the_hand(per_frame):
    r = Rig()
    _start(r)
    _move(r, 200, right=per_frame)
    r.hold(30, pose=TWO)
    expected = 200 * per_frame * r.ctl.scroll_units   # natural: hand right = wheel left
    assert -r.ptr.hscroll_total() == pytest.approx(
        expected, abs=r.ctl.scroll_units * r.ctl.scroll_deadzone + 1)


# -- axis lock -------------------------------------------------------------------------

@pytest.mark.parametrize("right, up, axis", [
    (0.004, 0.012, "scroll"),     # mostly up, a third sideways: up/down only
    (0.012, 0.004, "hscroll"),    # mostly sideways: sideways only
])
def test_slanted_swipe_sticks_to_its_main_axis(right, up, axis):
    r = Rig()
    _start(r)
    _move(r, 20, right=right, up=up)
    r.hold(10, pose=TWO)
    other = "hscroll" if axis == "scroll" else "scroll"
    assert r.ptr.of(axis) and r.ptr.of(other) == []


def test_axis_is_kept_for_the_whole_swipe():
    """The first movement decides; turning sideways later doesn't scroll sideways."""
    r = Rig()
    _start(r)
    _move(r, 8, up=0.01)
    _move(r, 15, right=0.01)
    assert r.ptr.of("scroll") and r.ptr.of("hscroll") == []
    assert r.last.scroll_axis == "vertical"


def test_a_new_touch_decides_its_axis_again():
    r = Rig()
    _start(r)
    _move(r, 8, up=0.01)
    r.hold(5, pose=PALM)                       # lift (slow: no fling)
    r.hold(12, pose=TWO)                       # touch again
    n = len(r.ptr.of("scroll"))
    _move(r, 10, right=0.01)
    assert r.ptr.of("hscroll") and len(r.ptr.of("scroll")) == n
    assert r.last.scroll_axis == "horizontal"


def test_axis_lock_doesnt_lose_the_first_movement():
    """Nothing scrolls until the axis is decided, then that movement is sent."""
    r = Rig()
    _start(r)
    _move(r, 1, up=0.004)                      # below the lock distance
    assert r.ptr.of("scroll") == [] and r.last.scroll_axis == ""
    _move(r, 1, up=0.004)
    r.hold(15, pose=TWO)
    assert abs(r.ptr.scroll_total()) == pytest.approx(0.008 * 25 * 120, abs=5.5)


def test_without_axis_lock_a_diagonal_scrolls_both_ways():
    r = Rig(make_config(scroll={"axis_lock": False}))
    _start(r)
    _move(r, 20, right=0.006, up=0.012)
    r.hold(15, pose=TWO)
    h, v = abs(r.ptr.hscroll_total()), abs(r.ptr.scroll_total())
    assert h > 0 and v == pytest.approx(2 * h, rel=0.05)
    assert r.last.scroll_axis == "both"


def test_horizontal_off_scrolls_up_down_only_with_no_lock_delay():
    """Exactly the old behaviour: up/down from the first frame, nothing to decide."""
    r = Rig(make_config(scroll={"horizontal": False}))
    _start(r)
    assert r.last.scroll_axis == "vertical"
    _move(r, 10, right=0.02)
    r.hold(10, pose=TWO)
    assert r.ptr.of("hscroll") == []


def test_status_reports_the_axis_only_while_scrolling():
    r = Rig()
    _start(r)
    _move(r, 5, right=0.01)
    assert r.last.scroll_axis == "horizontal"
    r.hold(5, pose=POINT)
    assert r.last.scroll_axis == "" and Status().scroll_axis == ""


# -- fling -------------------------------------------------------------------------

def test_fast_sideways_swipe_flings_sideways(rig):
    glide = _sideways_swipe_and_lift(rig, 0.05)
    assert len(glide) > 5 and all(u < 0 for _, u in glide)   # natural: right = wheel left
    assert "fling" in rig.events
    assert rig.ptr.of("scroll") == []


def test_slow_sideways_drag_does_not_fling(rig):
    assert _sideways_swipe_and_lift(rig, 0.005) == []


def test_stopping_before_lifting_does_not_fling_sideways(rig):
    assert _sideways_swipe_and_lift(rig, 0.05, stop_first=True) == []


def test_sideways_fling_can_be_turned_off():
    r = Rig(make_config(scroll={"fling": {"enabled": False}}))
    assert _sideways_swipe_and_lift(r, 0.05) == []


def test_sideways_glide_slows_down_and_stops(rig):
    _sideways_swipe_and_lift(rig, 0.05)
    rig.hold(150, pose=PALM)
    n = len(rig.ptr.of("hscroll"))
    rig.hold(30, pose=PALM)
    assert len(rig.ptr.of("hscroll")) == n
    steps = [abs(u) for _, u in rig.ptr.of("hscroll")[-20:]]
    assert steps[-1] <= steps[0]


def test_sideways_glide_is_boosted():
    def glide(boost):
        r = Rig(make_config(scroll={"fling": {"boost": boost}}))
        _start(r)
        _move(r, 6, right=0.05)
        return abs(_glide_total(r, "hscroll"))

    assert glide(3.0) == pytest.approx(3 * glide(1.0), rel=0.15)


def test_a_sideways_fling_is_as_fast_as_the_same_vertical_one():
    def glide(axis):
        r = Rig()
        _start(r)
        _move(r, 6, right=0.03 if axis == "hscroll" else 0.0,
              up=0.03 if axis == "scroll" else 0.0)
        return abs(_glide_total(r, axis))

    assert glide("hscroll") == pytest.approx(glide("scroll"), rel=0.02)


def test_peace_sign_catches_the_sideways_glide(rig):
    _sideways_swipe_and_lift(rig, 0.05)
    assert rig.ctl.gliding
    rig.step(pose=TWO)
    n = len(rig.ptr.of("hscroll"))
    rig.hold(15, pose=TWO)
    assert len(rig.ptr.of("hscroll")) == n


def test_click_stops_the_sideways_glide(rig):
    _sideways_swipe_and_lift(rig, 0.05)
    rig.point()
    rig.left_pinch()
    n = len(rig.ptr.of("hscroll"))
    rig.hold(10, pose=POINT, pinch_index=0.1)
    assert len(rig.ptr.of("hscroll")) == n


def test_hand_leaving_the_view_mid_sideways_swipe_flings(rig):
    _start(rig)
    _move(rig, 6, right=0.05)
    rig.no_hand()
    assert "fling" in rig.events
    assert _glide_total(rig, "hscroll", 20) != 0


# -- stacking ---------------------------------------------------------------------------

def test_quick_second_sideways_flick_stacks(rig):
    _sideways_swipe_and_lift(rig, 0.05)
    rig.hold(3, pose=PALM, anchor=(0.4, 0.5))     # bring the hand back
    rig.hold(3, pose=TWO)                          # catch
    _move(rig, 6, right=0.05)                      # flick again, same way
    rig.step(pose=PALM)
    assert "fling (stacked)" in rig.events


def test_flick_on_the_other_axis_doesnt_stack():
    """Catching an up/down glide, then flicking sideways: a separate fling."""
    r = Rig()
    _start(r)
    _move(r, 6, up=0.05)
    r.step(pose=PALM)
    r.hold(20, pose=PALM)                          # glide running
    r.hold(3, pose=TWO)                            # catch it
    _move(r, 6, right=0.05)
    r.step(pose=PALM)
    assert "fling (stacked)" not in r.events
    assert r.events.count("fling") == 2


def test_flick_back_the_other_way_doesnt_stack(rig):
    _sideways_swipe_and_lift(rig, 0.05)
    rig.hold(3, pose=TWO)
    _move(rig, 6, right=-0.05)                     # flick left after a right glide
    rig.step(pose=PALM)
    assert "fling (stacked)" not in rig.events


def test_misread_frame_mid_sideways_drag_does_not_stack():
    def run(misread_at):
        r = Rig()
        _start(r)
        for i in range(20):
            _move(r, 1, right=0.035, pose=NONE if i == misread_at else TWO)
        _move(r, 3, right=0.035, pose=PALM)
        r.hold(90, pose=PALM)
        return r

    clean, glitch = run(None), run(8)
    assert "fling (stacked)" not in glitch.events
    assert glitch.ptr.hscroll_total() == pytest.approx(clean.ptr.hscroll_total(), rel=0.05)


def test_diagonal_glide_speed_limit_keeps_its_direction():
    """max_notches_per_s caps the glide's overall speed, not each axis separately,
    so a diagonal glide keeps its angle."""
    r = Rig(make_config(scroll={"axis_lock": False,
                                "fling": {"max_notches_per_s": 20}}))
    _start(r)
    _move(r, 6, right=0.03, up=0.06)
    r.step(pose=PALM)
    r.hold(3, pose=PALM)
    vx, vy = r.ctl._fling_v
    assert (vx * vx + vy * vy) ** 0.5 <= 20 * 120 + 1
    assert vy / vx == pytest.approx(2.0, rel=0.1)


def test_glide_seconds_match_frame_time():
    """Sanity: the fake clock runs at 30 fps, like the camera."""
    assert DT == pytest.approx(1 / 30)
