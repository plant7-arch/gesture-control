"""The gesture state machine, driven frame by frame (30 fps, fake clock).

Each test plays a short hand performance and checks what the virtual devices would
have received. These are the behaviours the README promises.
"""

import pytest
from conftest import DT, Rig, make_config

from gesture_control import touchpad as tpm
from gesture_control.controller import Debouncer, GestureController, Mode, PinchDetector
from gesture_control.features import FIST, NONE, PALM, POINT, TWO

# -- Debouncer ----------------------------------------------------------------------


def test_debouncer_needs_n_steady_frames():
    d = Debouncer(3)
    assert [d.update(POINT) for _ in range(3)] == [NONE, NONE, POINT]


def test_debouncer_ignores_a_one_frame_flicker():
    d = Debouncer(3)
    for _ in range(3):
        d.update(POINT)
    seq = [PALM, POINT, PALM, PALM, POINT, POINT]
    assert [d.update(p) for p in seq] == [POINT] * 6


def test_debouncer_restarts_count_on_a_new_candidate():
    d = Debouncer(3)
    for p in (TWO, TWO, PALM, PALM):
        d.update(p)
    assert d.value == NONE
    assert d.update(PALM) == PALM


def test_debouncer_with_one_frame_reacts_at_once():
    d = Debouncer(1)
    assert d.update(FIST) == FIST


# -- PinchDetector ---------------------------------------------------------------------

def _pinch(p: PinchDetector, ratios, ok=True, can_start=True):
    return [p.update(r, ok, can_start) for r in ratios]


def test_pinch_must_be_seen_open_before_it_can_start():
    """A thumb resting on the finger (never seen apart) never clicks."""
    p = PinchDetector(0.28, 0.42, 3)
    out = _pinch(p, [0.1] * 20)
    assert not any(started for _, started, _ in out)
    assert not p.armed


def test_pinch_arms_then_fires_after_confirm_frames():
    p = PinchDetector(0.28, 0.42, 3)
    out = _pinch(p, [0.6, 0.1, 0.1, 0.1, 0.1])
    assert p.armed is False and p.active
    assert [s for _, s, _ in out] == [False, False, False, True, False]


def test_pinch_hysteresis_holds_between_thresholds():
    p = PinchDetector(0.28, 0.42, 1)
    _pinch(p, [0.6, 0.1])
    assert p.active
    out = _pinch(p, [0.30, 0.40, 0.41])       # above start, below release: still held
    assert all(active for active, _, _ in out)
    active, _, ended = p.update(0.5, True)
    assert not active and ended
    assert p.armed                              # ready for the next pinch


def test_pinch_count_restarts_if_fingers_open_early():
    p = PinchDetector(0.28, 0.42, 3)
    out = _pinch(p, [0.6, 0.1, 0.1, 0.35, 0.1, 0.1])
    assert not any(s for _, s, _ in out)


def test_pinch_disarms_when_not_ok_but_not_when_cannot_start():
    p = PinchDetector(0.28, 0.42, 1)
    _pinch(p, [0.6])
    _pinch(p, [0.1], can_start=False)            # e.g. finger curled: no start...
    assert p.armed and not p.active               # ...but still armed
    _pinch(p, [0.6], ok=False)                    # hand turned away: disarm
    assert not p.armed


def test_ongoing_pinch_survives_not_ok():
    """A drag isn't dropped when the hand turns."""
    p = PinchDetector(0.28, 0.42, 1)
    _pinch(p, [0.6, 0.1])
    active, _, _ = p.update(0.1, ok=False)
    assert active


# -- modes ----------------------------------------------------------------------------

def test_hand_appears_idle_until_pose_is_steady(rig):
    st = rig.step(pose=POINT)
    assert st.hand and st.mode == Mode.IDLE
    assert rig.hold(2, pose=POINT).mode == Mode.MOVE


@pytest.mark.parametrize("pose, mode", [(POINT, Mode.MOVE), (TWO, Mode.SCROLL),
                                        (PALM, Mode.IDLE), (FIST, Mode.IDLE)])
def test_pose_selects_mode(rig, pose, mode):
    assert rig.hold(5, pose=pose).mode == mode


def test_ambiguous_pose_keeps_the_current_mode(rig):
    rig.point()
    assert rig.hold(10, pose=NONE).mode == Mode.MOVE


def test_one_misread_frame_does_not_change_mode(rig):
    rig.point()
    rig.step(pose=PALM)
    assert rig.point(1).mode == Mode.MOVE


def test_no_hand_status(rig):
    st = rig.no_hand()
    assert not st.hand and st.mode == Mode.IDLE


# -- cursor (relative, the default) -------------------------------------------------------

def _travel(rig):
    """Cursor movement (screen fractions) recorded by the tablet output."""
    pts = rig.ptr.of("abs")
    if not pts:
        return 0.0, 0.0
    return pts[-1][1] - 0.5, pts[-1][2] - 0.5


@pytest.mark.parametrize("dx, dy, sx, sy", [(0.01, 0, 1, 0), (-0.01, 0, -1, 0),
                                            (0, 0.01, 0, 1), (0, -0.01, 0, -1)])
def test_cursor_follows_hand_direction(rig, dx, dy, sx, sy):
    rig.point()
    rig.move(15, dx, dy, pose=POINT)
    tx, ty = _travel(rig)
    assert (tx > 0.01) == (sx > 0) and (tx < -0.01) == (sx < 0)
    assert (ty > 0.01) == (sy > 0) and (ty < -0.01) == (sy < 0)


def test_still_hand_does_not_creep_the_cursor(rig):
    rig.point()
    for i in range(60):   # tracking jitter: +-0.0003 of the frame
        rig.step(pose=POINT, anchor=(0.5 + (0.0003 if i % 2 else -0.0003), 0.5))
    assert rig.ptr.of("abs") == []


def _run_move(steps, per_step, palm=0.16):
    r = Rig()
    r.point(palm=palm)
    r.move(steps, per_step, 0, pose=POINT, palm=palm)
    r.hold(15, pose=POINT, palm=palm)   # let the filter catch up
    return _travel(r)[0]


def test_fast_movement_goes_further_than_slow_over_the_same_distance():
    slow = _run_move(40, 0.005)   # 0.2 frame widths in 1.3 s
    fast = _run_move(4, 0.05)     # 0.2 frame widths in 0.13 s
    assert fast > 3 * slow > 0


def test_movement_feels_the_same_at_any_distance_from_the_camera():
    """Movement is measured in palm lengths: half the size, half the movement."""
    near = _run_move(15, 0.01, palm=0.16)
    far = _run_move(15, 0.005, palm=0.08)
    assert far == pytest.approx(near, rel=0.05)


def test_opening_the_palm_stops_the_cursor(rig):
    rig.point()
    rig.move(5, 0.01, pose=POINT)
    n = len(rig.ptr.of("abs"))
    rig.move(10, 0.01, pose=PALM)     # reposition the hand with an open palm
    assert len(rig.ptr.of("abs")) == n


def test_cursor_stays_on_screen(rig):
    rig.point()
    rig.move(30, 0.05, pose=POINT)
    assert rig.ptr.last_abs()[0] == pytest.approx(1.0)


def test_trackball_output_moves_equal_pixels_on_both_axes():
    """Equal hand movement in camera pixels = equal cursor movement in screen pixels."""
    cfg = make_config(cursor={"relative": {"output": "trackball",
                                           "trackball_counts": [1536, 864]}})
    r = Rig(cfg)
    r.point()
    r.move(15, 0.01, 0.01 * 640 / 480, pose=POINT)   # 45 degrees in camera pixels
    r.hold(15, pose=POINT)
    rel = r.ptr.of("rel")
    assert r.ptr.of("abs") == []
    x, y = sum(e[1] for e in rel), sum(e[2] for e in rel)
    assert x > 20 and y == pytest.approx(x, rel=0.05)


@pytest.mark.parametrize("tell_size, equal", [(True, True), (False, False)])
def test_vertical_movement_uses_the_cameras_real_frame_size(tell_size, equal):
    """The config asks for 640x480 but the camera delivers 1280x720. Up/down must be
    converted with the real shape, or it's a third too fast (0.75 vs 0.5625)."""
    cfg = make_config(cursor={"relative": {"output": "trackball",
                                           "trackball_counts": [1536, 864]}})
    r = Rig(cfg)
    if tell_size:
        r.ctl.set_frame_size(1280, 720)
    r.point()
    r.move(15, 0.01, 0.01 * 1280 / 720, pose=POINT)  # 45 degrees in 1280x720 pixels
    r.hold(15, pose=POINT)
    rel = r.ptr.of("rel")
    x, y = sum(e[1] for e in rel), sum(e[2] for e in rel)
    assert (y == pytest.approx(x, rel=0.05)) is equal
    if not equal:
        assert y == pytest.approx(x * 0.75 / 0.5625, rel=0.05)


def test_unknown_frame_size_is_ignored(rig):
    before = rig.ctl.frame_aspect
    rig.ctl.set_frame_size(0, 0)               # camera couldn't report its size
    assert rig.ctl.frame_aspect == before == 480 / 640


def test_hand_switch_does_not_jump_the_cursor():
    def jump(rebase):
        r = Rig()
        r.point()
        r.move(5, 0.005, pose=POINT)
        before = r.ptr.last_abs()
        r.step(pose=POINT, anchor=(r.anchor[0] + 0.3, 0.5), rebase=rebase)
        r.move(3, 0.005, pose=POINT)
        return r.ptr.last_abs()[0] - before[0]

    assert jump(rebase=False) > 0.1
    assert jump(rebase=True) < 0.03


# -- cursor (absolute mode) ---------------------------------------------------------------

@pytest.mark.parametrize("anchor, expect", [((0.8, 0.7), (0.9, 0.8)),     # region corner
                                            ((0.95, 0.95), (0.9, 0.8)),   # beyond: clamped
                                            ((0.5, 0.45), (0.45, 0.4))])  # centre
def test_absolute_mode_maps_region_to_calibrated_screen(anchor, expect):
    r = Rig(make_config(cursor={"mode": "absolute", "abs_scale": [0.9, 0.8]}))
    r.point(anchor=anchor)
    r.hold(60, pose=POINT)
    assert r.ptr.last_abs() == pytest.approx(expect, abs=0.01)


# -- clicks -------------------------------------------------------------------------------

def _buttons(rig, button="left"):
    return [v for kind, b, v in rig.ptr.of("btn") if b == button]


def test_left_click(rig):
    rig.point()
    st = rig.left_pinch()
    assert st.left_down and "left down" in rig.events
    rig.left_open()
    assert _buttons(rig) == [1, 0]
    assert rig.events == ["left down", "left up"]


def test_pinch_holds_cursor_still_while_click_lands(rig):
    rig.point()
    rig.left_pinch(close_frames=3)        # press on this frame
    n = len(rig.ptr.of("abs"))
    rig.move(4, 0.01, pose=POINT, pinch_index=0.1)   # within click_freeze_ms (180)
    assert len(rig.ptr.of("abs")) == n


def test_no_click_when_thumb_was_never_seen_apart(rig):
    rig.point(pinch_index=0.1)
    rig.hold(20, pose=POINT, pinch_index=0.1)
    assert _buttons(rig) == []


def test_no_click_with_hand_turned_away(rig):
    rig.point(facing=0.3)
    rig.hold(2, pose=POINT, pinch_index=1.0, facing=0.3)
    st = rig.hold(5, pose=POINT, pinch_index=0.1, facing=0.3)
    assert st.pinch_blocked and _buttons(rig) == []


def test_no_click_from_a_curled_index_finger(rig):
    rig.point(reach_index=0.8)
    rig.hold(2, pose=POINT, pinch_index=1.0, reach_index=0.8)
    rig.hold(5, pose=POINT, pinch_index=0.1, reach_index=0.8)
    assert _buttons(rig) == []


def test_no_click_outside_move_mode(rig):
    rig.hold(5, pose=TWO)
    rig.hold(2, pose=TWO, pinch_index=1.0)
    rig.hold(5, pose=TWO, pinch_index=0.1)
    assert _buttons(rig) == []


def test_armed_flag_is_reported(rig):
    rig.point(pinch_index=0.1)
    assert not rig.last.left_armed
    assert rig.step(pose=POINT, pinch_index=1.0).left_armed


def test_double_click(rig):
    rig.point()
    rig.left_pinch()
    rig.left_open()
    rig.left_pinch()
    rig.left_open()
    assert rig.events == ["left down", "left up", "double click", "left up"]
    assert _buttons(rig) == [1, 0, 1, 0]


def test_double_click_adds_a_click_if_the_os_window_ran_out():
    """Second pinch after the OS double-click time (400 ms) but within window_ms:
    a quick extra click is sent so the OS still sees a double click."""
    r = Rig(make_config(double_click={"window_ms": 1500}))
    r.point()
    r.left_pinch()
    r.left_open(20)                      # ~0.7 s apart
    r.left_pinch()
    assert "double click" in r.events
    assert _buttons(r) == [1, 0, 1, 0, 1]


def test_slow_second_pinch_is_a_new_click(rig):
    rig.point()
    rig.left_pinch()
    rig.left_open(30)                     # 1 s > window_ms 700
    rig.left_pinch()
    assert rig.events == ["left down", "left up", "left down"]


def test_double_click_needs_the_cursor_near_the_first_click(rig):
    rig.point()
    rig.left_pinch()
    rig.left_open()
    rig.move(6, 0.02, pose=POINT)        # drifted far
    rig.left_pinch()
    assert "double click" not in rig.events


def test_double_click_snaps_back_to_the_first_click(rig):
    rig.point()
    rig.move(10, 0.005, pose=POINT)
    rig.left_pinch()
    first = rig.ptr.last_abs()
    rig.left_open()
    rig.move(8, 0.0015, pose=POINT)     # small drift (after the click freeze)...
    drift = rig.ptr.last_abs()[0] - first[0]
    assert 0.001 < drift < 0.02         # ...that really moved, within max_move
    rig.left_pinch(close_frames=3)
    assert "double click" in rig.events
    assert rig.ptr.last_abs() == pytest.approx(first)


def test_right_click(rig):
    rig.point()
    rig.hold(2, pose=POINT, pinch_middle=1.0)
    rig.hold(4, pose=POINT, pinch_middle=0.1)
    assert "right click" in rig.events
    assert _buttons(rig, "right") == [1, 0]
    assert _buttons(rig) == []


def test_right_click_is_blocked_during_a_left_pinch(rig):
    rig.point()
    rig.left_pinch()
    rig.hold(2, pose=POINT, pinch_index=0.1, pinch_middle=1.0)
    rig.hold(4, pose=POINT, pinch_index=0.1, pinch_middle=0.1)
    assert _buttons(rig, "right") == []


# -- drag ------------------------------------------------------------------------------

def test_drag_moves_with_the_button_held(rig):
    rig.point()
    rig.left_pinch()
    start = rig.ptr.last_abs() or (0.5, 0.5)
    rig.hold(6, pose=POINT, pinch_index=0.1)            # wait out the click freeze
    rig.move(15, 0.01, pose=POINT, pinch_index=0.1)
    assert rig.last.left_down
    assert rig.ptr.last_abs()[0] > start[0] + 0.02
    rig.left_open()
    assert _buttons(rig) == [1, 0]


def test_drag_survives_a_tight_pinch_reading_as_a_fist(rig):
    """A tight pinch can look like a fist: that must not drop the drag or pause."""
    rig.point()
    rig.left_pinch()
    st = rig.hold(45, pose=FIST, pinch_index=0.1)       # longer than fist_hold_s
    assert st.left_down and st.mode == Mode.MOVE and not st.paused
    assert "paused" not in rig.events


def test_drag_continues_when_the_pose_changes(rig):
    rig.point()
    rig.left_pinch()
    st = rig.hold(10, pose=PALM, pinch_index=0.1)
    assert st.left_down and st.mode == Mode.MOVE


def test_button_released_when_the_hand_is_lost(rig):
    rig.point()
    rig.left_pinch()
    assert rig.no_hand(7).left_down               # brief dropout: keep holding
    st = rig.no_hand(1)                           # lost_hand_release_frames = 8
    assert not st.left_down
    assert "hand lost: released" in rig.events


def test_leaving_move_mode_releases_buttons(rig):
    rig.point()
    rig.left_pinch()
    rig.ctl._enter(Mode.IDLE)
    assert not rig.ptr.any_down()


# -- scroll ------------------------------------------------------------------------------

def _start_scroll(rig):
    rig.hold(12, pose=TWO)        # debounce + settle_ms (200)
    rig.ptr.log.clear()


@pytest.mark.parametrize("direction, sign", [("natural", -1), ("traditional", 1)])
def test_scroll_direction(direction, sign):
    """natural (phone): hand up -> content up -> wheel down."""
    r = Rig(make_config(scroll={"direction": direction}))
    _start_scroll(r)
    r.move(15, dy=-0.01, pose=TWO)            # hand moves up
    r.hold(10, pose=TWO)
    total = r.ptr.scroll_total()
    assert total * sign > 0
    # 0.15 frame heights x 25 notches per height x 120 = 450 units
    assert 0.8 * 450 <= abs(total) <= 450


def test_still_peace_sign_does_not_scroll(rig):
    _start_scroll(rig)
    rig.hold(30, pose=TWO)
    assert rig.ptr.of("scroll") == []


def test_movement_during_settle_is_ignored(rig):
    rig.hold(3, pose=TWO)                      # just entered SCROLL
    rig.move(4, dy=-0.02, pose=TWO)            # hand still arriving
    assert rig.ptr.of("scroll") == []


def _swipe_and_lift(rig, per_frame, frames=6, stop_first=False):
    _start_scroll(rig)
    rig.move(frames, dy=-per_frame, pose=TWO)
    if stop_first:
        rig.hold(10, pose=TWO)
    n = len(rig.ptr.of("scroll"))
    rig.step(pose=PALM, anchor=(rig.anchor[0], rig.anchor[1] - per_frame))
    rig.hold(20, pose=PALM)                    # hand still, finger off the glass
    return rig.ptr.of("scroll")[n:]


def test_fast_swipe_flings(rig):
    after = _swipe_and_lift(rig, 0.05)
    assert "fling" in rig.events
    assert len(after) > 10
    assert all(units < 0 for _, units in after)     # same way as the drag (natural)


def test_slow_drag_does_not_fling(rig):
    after = _swipe_and_lift(rig, 0.004, frames=20)
    assert "fling" not in rig.events and after == []


def test_stopping_before_lifting_does_not_fling(rig):
    after = _swipe_and_lift(rig, 0.05, stop_first=True)
    assert "fling" not in rig.events and after == []


def test_fling_can_be_turned_off():
    r = Rig(make_config(scroll={"fling": {"enabled": False}}))
    assert _swipe_and_lift(r, 0.05) == []


def test_glide_slows_down_and_stops(rig):
    _swipe_and_lift(rig, 0.05)
    rig.hold(150, pose=PALM)                   # 5 s
    n = len(rig.ptr.of("scroll"))
    rig.hold(30, pose=PALM)
    assert len(rig.ptr.of("scroll")) == n
    steps = [abs(u) for _, u in rig.ptr.of("scroll")[-20:]]
    assert steps[-1] <= steps[0]


def test_glide_is_boosted():
    """fling.boost: the glide covers boost x the distance of an unboosted glide."""
    def total_after_lift(boost):
        r = Rig(make_config(scroll={"fling": {"boost": boost}}))
        _start_scroll(r)
        r.move(6, dy=-0.05, pose=TWO)
        n = len(r.ptr.of("scroll"))
        r.hold(200, pose=PALM)
        return abs(sum(u for _, u in r.ptr.of("scroll")[n:]))

    one, three = total_after_lift(1.0), total_after_lift(3.0)
    assert three == pytest.approx(3 * one, rel=0.15)


def test_peace_sign_catches_the_glide(rig):
    _swipe_and_lift(rig, 0.05)
    assert rig.ctl._fling_v != 0
    rig.step(pose=TWO)
    n = len(rig.ptr.of("scroll"))
    rig.hold(15, pose=TWO)                     # hand still on the glass
    assert len(rig.ptr.of("scroll")) == n


def test_click_stops_the_glide(rig):
    _swipe_and_lift(rig, 0.05)
    rig.point()
    rig.left_pinch()
    n = len(rig.ptr.of("scroll"))
    rig.hold(10, pose=POINT, pinch_index=0.1)
    assert len(rig.ptr.of("scroll")) == n


def test_quick_second_flick_stacks(rig):
    _swipe_and_lift(rig, 0.05)
    rig.hold(3, pose=PALM, anchor=(0.5, 0.6))           # bring the hand back down
    rig.hold(3, pose=TWO)                               # catch
    rig.move(6, dy=-0.05, pose=TWO)                     # flick again, same way
    rig.step(pose=PALM)
    assert "fling (stacked)" in rig.events


def _long_drag_then_flick(misread_at: int | None) -> Rig:
    """One upward drag of 20 frames (optionally with one misread frame), released
    while still moving, then left to glide out."""
    r = Rig()
    _start_scroll(r)
    for i in range(20):
        r.move(1, dy=-0.035, pose=NONE if i == misread_at else TWO)
    r.move(3, dy=-0.035, pose=PALM)
    r.hold(90, pose=PALM)
    return r


def test_misread_frame_mid_drag_does_not_stack_the_fling():
    """A single frame read as another pose mid-drag starts and catches a one-frame
    glide. That isn't a real catch, so the final fling must not stack on it."""
    clean, glitch = _long_drag_then_flick(None), _long_drag_then_flick(8)
    assert "fling (stacked)" not in glitch.events
    assert glitch.ptr.scroll_total() == pytest.approx(clean.ptr.scroll_total(), rel=0.05)


@pytest.mark.parametrize("off_frames, stacks", [(2, False), (5, True)])
def test_only_a_glide_that_ran_a_while_counts_as_caught(off_frames, stacks):
    """The catch counts once the glide has run MIN_CATCH_AGE_S (0.1 s = 3 frames)."""
    r = Rig()
    _start_scroll(r)
    r.move(6, dy=-0.05, pose=TWO)
    r.step(pose=PALM)                      # lift: glide starts
    r.hold(off_frames - 1, pose=PALM)
    r.hold(3, pose=TWO)                    # catch
    r.move(6, dy=-0.05, pose=TWO)          # flick again, same way
    r.step(pose=PALM)
    assert ("fling (stacked)" in r.events) is stacks


def test_scroll_lift_when_hand_leaves_view_mid_swipe(rig):
    _start_scroll(rig)
    rig.move(6, dy=-0.05, pose=TWO)
    rig.no_hand()
    assert "fling" in rig.events


# -- pause -------------------------------------------------------------------------------

def _fist(rig, seconds=1.1):
    return rig.hold(int(seconds / DT) + 1, pose=FIST)


def test_fist_held_pauses_and_resumes(rig):
    st = rig.hold(15, pose=FIST)
    assert 0.3 < st.fist_progress < 0.7 and not st.paused
    st = _fist(rig)
    assert st.paused and rig.events == ["paused"]
    rig.hold(3, pose=PALM)
    st = _fist(rig)
    assert not st.paused and rig.events == ["paused", "resumed"]


def test_keeping_the_fist_does_not_toggle_again(rig):
    _fist(rig, 3.5)
    assert rig.events == ["paused"]


def test_short_fist_does_nothing(rig):
    rig.hold(20, pose=FIST)
    rig.hold(3, pose=PALM)
    rig.hold(20, pose=FIST)
    assert rig.events == [] and not rig.last.paused


def test_paused_ignores_gestures(rig):
    _fist(rig)
    rig.hold(3, pose=PALM)
    rig.point()
    rig.move(10, 0.01, pose=POINT)
    rig.left_pinch()
    rig.hold(15, pose=TWO)
    rig.move(10, dy=-0.03, pose=TWO)
    assert rig.ptr.log == []


def test_start_paused():
    r = Rig(make_config(pause={"start_paused": True}))
    assert r.point().paused


def test_pause_key_toggles(rig):
    rig.ctl.toggle_pause()
    assert rig.step(pose=PALM).paused


# -- zoom (two hands) ---------------------------------------------------------------------

@pytest.fixture
def zoom_rig(monkeypatch):
    def make(**overrides):
        method = overrides.get("zoom", {}).get("method", "touchpad")
        r = Rig(make_config(**overrides), keys=method != "touchpad",
                touchpad=method == "touchpad")
        monkeypatch.setattr(tpm, "_now", lambda: r.t)
        return r
    return make


def _zoom(r, spreads, pose=POINT, **kw):
    for s in spreads:
        r.step(pose=pose, zoom=(True, s), **kw)
    return r.last


def _ramp(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_two_pointing_hands_zoom_like_a_touchpad(zoom_rig):
    """Hands spread 1.5x = virtual fingers spread 1.5x."""
    r = zoom_rig()
    st = _zoom(r, [3.0] * 10)                   # debounce + settle
    assert st.mode == Mode.ZOOM and "zoom" in r.events
    assert r.tp.is_down
    start = r.tp.separation
    _zoom(r, _ramp(3.0, 4.5, 15) + [4.5] * 20)
    assert r.tp.separation == pytest.approx(1.5 * start, rel=0.05)
    assert all(b[1] >= a[1] for a, b in zip(r.tp.events[1:], r.tp.events[2:]))


def test_hands_together_zoom_out(zoom_rig):
    r = zoom_rig()
    _zoom(r, [4.0] * 10)
    start = r.tp.separation
    _zoom(r, _ramp(4.0, 3.0, 15) + [3.0] * 20)
    assert r.tp.separation == pytest.approx(0.75 * start, rel=0.05)


def test_zoom_invert(zoom_rig):
    r = zoom_rig(zoom={"invert": True})
    _zoom(r, [3.0] * 10)
    start = r.tp.separation
    _zoom(r, _ramp(3.0, 4.5, 15) + [4.5] * 20)
    assert r.tp.separation < start


def test_zoom_ends_and_lifts_the_fingers(zoom_rig):
    r = zoom_rig()
    _zoom(r, [3.0] * 20)
    st = r.hold(3, pose=POINT)                  # second hand gone
    assert st.mode != Mode.ZOOM and "zoom end" in r.events
    assert r.hold(5, pose=POINT).mode == Mode.MOVE   # the remaining hand points on
    r.hold(10, pose=PALM)
    assert not r.tp.is_down


def test_fingers_lift_when_both_hands_leave(zoom_rig):
    r = zoom_rig()
    _zoom(r, [3.0] * 20)
    r.no_hand(10)
    assert not r.tp.is_down and r.last.mode == Mode.IDLE


def test_no_clicks_or_cursor_movement_while_zooming(zoom_rig):
    r = zoom_rig()
    r.point()
    _zoom(r, [3.0] * 10)
    r.ptr.log.clear()
    _zoom(r, [3.0] * 2, pinch_index=1.0)
    for i in range(10):
        r.step(pose=POINT, pinch_index=0.1, zoom=(True, 3.0),
               anchor=(0.5 + 0.02 * i, 0.5))
    assert r.ptr.log == []


def test_zoom_does_not_start_during_a_drag(zoom_rig):
    r = zoom_rig()
    r.point()
    r.left_pinch()
    st = _zoom(r, [3.0] * 10, pinch_index=0.1)
    assert st.mode == Mode.MOVE and st.left_down
    assert not r.tp.is_down


def test_one_pointing_hand_is_not_a_zoom(zoom_rig):
    r = zoom_rig()
    for _ in range(10):
        r.step(pose=POINT, zoom=(False, 3.0))   # other hand not pointing
    assert r.last.mode == Mode.MOVE


def test_ctrl_scroll_zoom(zoom_rig):
    r = zoom_rig(zoom={"method": "ctrl_scroll"})
    _zoom(r, [3.0] * 10)
    _zoom(r, _ramp(3.0, 3.0 * 1.15 ** 2, 10) + [3.0 * 1.15 ** 2] * 5)
    assert r.keys.is_down("ctrl")
    assert r.ptr.scroll_total() == pytest.approx(2 * 120, rel=0.15)  # 2 notches in
    _zoom(r, [3.0 * 1.15 ** 2] * 20)            # hands still: Ctrl let go
    assert not r.keys.is_down("ctrl")


def test_keys_zoom_taps_whole_steps(zoom_rig, monkeypatch):
    monkeypatch.setattr("gesture_control.pointer.time.sleep", lambda s: None)
    r = zoom_rig(zoom={"method": "keys"})
    _zoom(r, [3.0] * 10)
    end = 3.0 / 1.15 ** 2.4                    # 2.4 steps together: 2 whole taps
    _zoom(r, _ramp(3.0, end, 10) + [end] * 40)
    taps = [k for k, v in r.keys.log if v == 1 and k != "ctrl"]
    assert taps == ["minus", "minus"]
    assert not r.keys.is_down("ctrl")


def test_zoom_off_without_a_device():
    r = Rig()                                  # no touchpad given
    for _ in range(10):
        r.step(pose=POINT, zoom=(True, 3.0))
    assert r.last.mode == Mode.MOVE


def test_zoom_blocked_when_touchpad_is_off(zoom_rig):
    class Off:
        usable = False
        state = "disabled"

        def message(self):
            return "Zoom is off: the touchpad is turned off"

    r = zoom_rig()
    r.ctl.tp_settings = Off()
    st = _zoom(r, [3.0] * 10 + _ramp(3.0, 4.5, 10))
    assert st.zoom_blocked and not r.tp.is_down
    assert "zoom unavailable: touchpad is off" in r.events


@pytest.mark.parametrize("state", ["disabled", "disabled-on-external-mouse"])
def test_zoom_blocked_is_flagged_once_per_block_with_its_reason(zoom_rig, state):
    """The app prints the message when zoom_blocked_new is set, and the preview picks
    its text from zoom_blocked_state; neither depends on message wording."""
    class Off:
        usable = False

        def __init__(self, s):
            self.state = s

        def message(self):
            return "any wording"

    r = zoom_rig()
    r.ctl.tp_settings = Off(state)
    flags = []
    for s in [3.0] * 10 + _ramp(3.0, 4.5, 10):
        st = r.step(pose=POINT, zoom=(True, s))
        flags.append(st.zoom_blocked_new)
        if st.zoom_blocked:
            assert st.zoom_blocked_state == state
    assert flags.count(True) == 1


def test_overlay_names_the_touchpad_setting_from_the_state():
    """The preview's zoom-off line follows zoom_blocked_state, not the message text."""
    import numpy as np

    from gesture_control import overlay as ov
    from gesture_control.controller import Status

    texts = []
    o = ov.Overlay.__new__(ov.Overlay)          # no window needed for draw()
    o.show_region = o.show_landmarks = False
    o.pinch_start, o.pinch_release = 0.28, 0.42
    real = ov._text
    ov._text = lambda img, s, *a, **k: texts.append(s)
    try:
        for state in ("disabled", "disabled-on-external-mouse"):
            st = Status(zoom_blocked="any wording", zoom_blocked_state=state)
            o.draw(np.zeros((480, 640, 3), np.uint8), None, st, 30.0, 10.0)
    finally:
        ov._text = real
    lines = [t for t in texts if t.startswith("Zoom is off")]
    assert "turned off in Settings" in lines[0]
    assert "mouse is connected" in lines[1]


# -- construction -------------------------------------------------------------------------

def test_controller_builds_from_the_shipped_config(tmp_path):
    import shutil

    from conftest import RecordingPointer

    from gesture_control.config import DEFAULT_CONFIG_PATH, load_config
    shutil.copy(DEFAULT_CONFIG_PATH, tmp_path / "gestures.yaml")
    cfg = load_config(tmp_path / "gestures.yaml")
    r = Rig(cfg)
    assert r.point().mode == Mode.MOVE
    GestureController(cfg, RecordingPointer())


# -- actions (every action, for code that follows the user, e.g. a guide) ------------------

def _check_events_within_actions(r):
    """events are the actions that aren't quiet, in the same order."""
    it = iter(r.actions)
    assert all(e in it for e in r.events)


def test_scroll_steps_are_actions_but_not_events(rig):
    _start_scroll(rig)
    rig.move(15, dy=-0.01, pose=TWO)           # hand up, natural: content up
    assert "scroll down" in rig.actions and "scroll up" not in rig.actions
    assert not any(e.startswith("scroll") for e in rig.events)   # -v output unchanged
    rig.move(15, dy=0.01, pose=TWO)
    assert "scroll up" in rig.actions
    _check_events_within_actions(rig)


def test_zoom_steps_are_actions_but_not_events(zoom_rig):
    r = zoom_rig()
    _zoom(r, [3.0] * 10 + _ramp(3.0, 4.5, 15) + _ramp(4.5, 3.0, 15))
    assert "zoom in" in r.actions and "zoom out" in r.actions
    assert r.events == ["zoom"]
    _check_events_within_actions(r)


def test_hand_switch_is_an_action_but_not_an_event(rig):
    rig.point()
    st = rig.step(pose=POINT, rebase=True)
    assert st.actions == ["hand switch"] and st.events == []


def test_clicks_are_both(rig):
    rig.point()
    rig.left_pinch()
    rig.left_open()
    assert rig.actions == rig.events == ["left down", "left up"]


def test_actions_are_per_frame(rig):
    _start_scroll(rig)
    st = rig.move(5, dy=-0.02, pose=TWO)
    assert st.actions == ["scroll down"]              # this frame only, not a history
    rig.hold(30, pose=TWO)                            # hand stopped; smoothing settles
    assert rig.step(pose=TWO).actions == []


# -- glitches and stalls ------------------------------------------------------------------

def test_one_missed_frame_of_the_second_hand_does_not_end_the_zoom(zoom_rig):
    """The tracker sometimes misses a hand for a frame: the zoom (and its fingers on the
    virtual touchpad) carries on, like it does when a pose is misread for a frame."""
    r = zoom_rig()
    _zoom(r, [3.0] * 20)
    r.step(pose=POINT, zoom=None)               # second hand missed for one frame
    st = _zoom(r, [3.0] * 3)
    assert st.mode == Mode.ZOOM
    assert "zoom end" not in r.events
    assert [e[0] for e in r.tp.events].count("down") == 1


def test_missed_frame_keeps_ctrl_scroll_zoom_going(zoom_rig):
    r = zoom_rig(zoom={"method": "ctrl_scroll"})
    _zoom(r, [3.0] * 10)
    r.step(pose=POINT, zoom=None)
    _zoom(r, _ramp(3.0, 3.0 * 1.15 ** 2, 10))
    assert r.events.count("zoom") == 1 and "zoom end" not in r.events


def test_camera_stall_releases_a_drag(rig):
    rig.point()
    rig.left_pinch()
    assert rig.ptr.is_down("left")
    st = rig.ctl.stalled(rig.t + 1.0)           # no frames: update() never runs
    assert not rig.ptr.is_down("left")
    assert st.mode == Mode.IDLE and "camera stalled: released" in st.events


def test_camera_stall_lets_go_of_ctrl(zoom_rig):
    r = zoom_rig(zoom={"method": "ctrl_scroll"})
    _zoom(r, [3.0] * 10)
    _zoom(r, _ramp(3.0, 3.0 * 1.15 ** 2, 10))
    assert r.keys.is_down("ctrl")
    r.ctl.stalled(r.t + 1.0)
    assert not r.keys.is_down("ctrl")


def test_camera_stall_lifts_touchpad_fingers(zoom_rig):
    r = zoom_rig()
    _zoom(r, [3.0] * 20)
    assert r.tp.is_down
    r.ctl.stalled(r.t + 1.0)
    assert not r.tp.is_down


def test_camera_stall_while_idle_does_nothing(rig):
    rig.hold(5, pose=PALM)
    st = rig.ctl.stalled(rig.t + 1.0)
    assert st.events == [] and rig.ptr.log == []


@pytest.mark.parametrize("boost", [0.1, 0.2, 0.3, 0.5, 1.0, 2.0])
def test_glide_never_turns_round(boost):
    """However low fling.boost is (it may be 0.1), the glide only ever moves the page
    the way it was flung."""
    r = Rig(make_config(scroll={"fling": {"boost": boost}}))
    c = r.ctl
    c._fling_v0 = c._fling_v = 6000.0
    c._fling_age, c._fling_t, c._carry = 0.0, 0.0, 0.0
    t = 0.0
    while c._fling_v and t < 10:
        t += DT
        c._apply_fling(t)
    steps = [e[1] for e in r.ptr.of("scroll")]
    assert steps and all(s > 0 for s in steps)
