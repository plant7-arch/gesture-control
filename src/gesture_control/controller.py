"""Gesture state machine: turns per-frame hand features into pointer actions.

Modes
  IDLE   - hand visible but not controlling anything (palm, unknown pose)
  MOVE   - "point" pose: cursor follows the hand (absolute) or its movement
           (relative, touchpad-like); pinches click/drag
  ZOOM   - two hands, both pointing: spreading them apart zooms in, bringing them
           together zooms out. Default: a true two-finger pinch on a virtual touchpad;
           optionally Ctrl+wheel or Ctrl+= / Ctrl+- taps
  SCROLL - "two" pose: hand movement scrolls, up/down and left/right, phone-style by
           default: the content follows the hand, and leaving the pose mid-swipe
           flings it. Each swipe sticks to one axis unless scroll.axis_lock is off

Rules that keep it from misfiring
  * poses must be stable for `pose.debounce_frames` frames before they count
  * ambiguous frames ("none") keep the current mode instead of dropping it
  * pinches use hysteresis (start < release) and need `confirm_frames` frames
  * a pinch only counts if the finger is reaching forward (not curled in a fist)
  * while the left button is held (drag), MOVE is latched until you let go
  * a fist held for `pause.fist_hold_s` toggles pause; hold again to resume
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import touchpad as tpm
from .features import FIST, NONE, POINT, TWO, HandFeatures
from .filters import OneEuroFilter
from .pointer import HI_RES_PER_NOTCH, VirtualKeys, VirtualPointer
from .scrolling import Scroller

# A glide must have run at least this long for catching it to count towards stacking.
# Leaving the peace sign and making it again takes a person well over 0.1 s; a glide
# caught sooner was started by a misread frame in the middle of one drag, and stacking
# on it would make the next fling overshoot.
MIN_CATCH_AGE_S = 0.1

# With scroll.axis_lock, a swipe's axis is decided once the hand has moved this far
# (frame heights, about 3 px at 480 px) from where the touch began. Until then nothing
# scrolls; the movement isn't lost, it's sent along the chosen axis right after.
AXIS_LOCK_DIST = 0.006

Vec = tuple[float, float]  # (horizontal, vertical); + = hand right / hand up
ZERO: Vec = (0.0, 0.0)


def _norm(v: Vec) -> float:
    return math.hypot(v[0], v[1])

class Mode(str, Enum):
    IDLE = "idle"
    MOVE = "move"
    SCROLL = "scroll"
    ZOOM = "zoom"


@dataclass
class Status:
    hand: bool = False
    paused: bool = False
    mode: Mode = Mode.IDLE
    raw_pose: str = NONE
    pose: str = NONE
    left_down: bool = False
    pinch_index: float | None = None
    pinch_middle: float | None = None
    facing: float | None = None
    pinch_blocked: bool = False  # hand turned too far for a reliable pinch
    left_armed: bool = False     # fingers seen apart: the next close will click
    left_block: str = ""         # fingertips closed but not clicking: why
    right_armed: bool = False
    fist_progress: float = 0.0
    zoom_spread: float | None = None  # index-tip distance in palm lengths (two hands)
    zoom_hands: bool = False          # two pointing hands seen this frame
    zoom_blocked: str = ""            # why zoom can't work right now (full message)
    zoom_blocked_state: str = ""      # the touchpad setting behind it ("disabled", ...)
    zoom_blocked_new: bool = False    # zoom was just found blocked: print the message once
    scroll_axis: str = ""             # scrolling: "vertical", "horizontal", "both" or ""
    last_action: str = ""
    # Actions this frame. events: the ones worth printing (-v), once per gesture.
    # actions: every action, including the frequent "quiet" ones (each scroll or zoom
    # step, a hand switch), in order; for anything that follows what the user does.
    events: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


class Debouncer:
    def __init__(self, frames: int, initial: str = NONE):
        self.frames = max(1, int(frames))
        self.value = initial
        self._candidate = initial
        self._count = 0

    def update(self, v: str) -> str:
        if v == self.value:
            self._candidate, self._count = v, 0
        elif v == self._candidate:
            self._count += 1
            if self._count >= self.frames:
                self.value, self._count = v, 0
        else:
            self._candidate, self._count = v, 1
            if self.frames == 1:
                self.value, self._count = v, 0
        return self.value

    def reset(self) -> None:
        self.value = self._candidate = NONE
        self._count = 0


class PinchDetector:
    """Pinch with hysteresis, confirmation frames and arming. Returns (active, started, ended).

    Arming: a pinch can only start after the fingers have been seen clearly apart
    (above `release`) while `ok` holds. So fingers that merely appear to touch as the
    hand turns into view never fire; a real pinch is an observed closing motion.
    """

    def __init__(self, start: float, release: float, confirm_frames: int):
        self.start, self.release_t = float(start), float(release)
        self.confirm = max(1, int(confirm_frames))
        self.active = False
        self.armed = False
        self._count = 0

    def update(self, ratio: float, ok: bool, can_start: bool = True) -> tuple[bool, bool, bool]:
        """ok: conditions for arming and starting (e.g. hand faces the camera).
        can_start: extra condition only for the moment the pinch starts; failing it
        does not disarm."""
        if not self.active:
            if not ok:
                self.armed, self._count = False, 0
                return False, False, False
            if ratio > self.release_t:
                self.armed = True
            if self.armed and ratio < self.start and can_start:
                self._count += 1
                if self._count >= self.confirm:
                    self.active, self.armed, self._count = True, False, 0
                    return True, True, False
            else:
                self._count = 0
            return False, False, False
        if ratio > self.release_t:
            self.active = False
            self.armed = True  # fingers are apart again: ready for the next pinch
            return False, False, True
        return True, False, False

    def reset(self) -> None:
        self.active, self.armed, self._count = False, False, 0


class GestureController:
    def __init__(self, cfg: dict[str, Any], pointer: VirtualPointer,
                 keys: VirtualKeys | None = None, touchpad=None, touchpad_settings=None,
                 scroller: Scroller | None = None):
        self.cfg = cfg
        self.pointer = pointer
        self.keys = keys
        self.touchpad = touchpad
        self.tp_settings = touchpad_settings
        # Where scrolling goes: a virtual touchpad swipe (app.py) or the mouse wheel.
        self.scroller = scroller if scroller is not None else Scroller(pointer)
        self._now = 0.0  # time of the current update() / stalled() call
        zc = cfg["zoom"]
        self.zoom_method = zc["method"]
        needs = touchpad if self.zoom_method == "touchpad" else keys
        self.zoom_enabled = bool(zc["enabled"]) and needs is not None
        tc = zc["touchpad"]
        self.tp_start_mm = float(tc["start_mm"])
        self.tp_gain = float(tc["gain"])
        self._tp_ref: float | None = None   # log spread when the pinch started
        self._tp_base = self.tp_start_mm      # finger separation when the pinch started
        self._blocked_said = False
        self._blocked_new = False
        self._zoom_blocked_state = ""
        self.zoom_debounce = Debouncer(cfg["pose"]["debounce_frames"])
        self.zoom_step = math.log(float(zc["notch_ratio"]))
        self.zoom_sign = -1.0 if zc["invert"] else 1.0
        self.zoom_settle_s = float(zc["settle_ms"]) / 1000.0
        self.zoom_deadzone = float(zc["deadzone"])
        self.zoom_ctrl_idle_s = float(zc["ctrl_release_ms"]) / 1000.0
        self.zoom_filter = OneEuroFilter(**zc["filter"])
        self._zoom_prev: float | None = None
        self._zoom_status_blocked = ""
        self._zoom_acc = 0.0
        self._zoom_last_step = -1e9
        pc, cc, sc = cfg["pinch"], cfg["cursor"], cfg["scroll"]

        self.pose = Debouncer(cfg["pose"]["debounce_frames"])
        self.left = PinchDetector(pc["start"], pc["release"], pc["confirm_frames"])
        self.right = PinchDetector(pc["start"], pc["release"], pc["confirm_frames"])
        self.min_reach = float(pc["min_finger_reach"])
        self.right_min_reach = float(pc["right_min_reach"])
        self.min_facing = float(pc["min_facing"])
        self._block_frames, self._block_said = 0, False

        self.cursor_filter = OneEuroFilter(**cc["filter"])
        self.scroll_filter = OneEuroFilter(**sc["filter"])
        r = cc["region"]
        self.region = (float(r["x_min"]), float(r["x_max"]), float(r["y_min"]), float(r["y_max"]))
        self.abs_mode = cc["mode"] == "absolute"
        self.abs_scale = tuple(cc.get("abs_scale", (1.0, 1.0)))
        self.deadzone = float(cc["deadzone"])
        rc = cc["relative"]
        self.rel_gain_slow, self.rel_gain_fast = float(rc["gain_slow"]), float(rc["gain_fast"])
        self.rel_speed_slow, self.rel_speed_fast = float(rc["speed_slow"]), float(rc["speed_fast"])
        self.rel_min_speed = float(rc["min_speed"])
        self.rel_filter = OneEuroFilter(**rc["filter"])
        # app.py resolves "auto" before this; unresolved (tests, tools) means tablet.
        self.rel_output = "tablet" if rc["output"] == "auto" else rc["output"]
        # app.py resolves "auto" from the display; unresolved (tests) means 16:10.
        sa = rc["screen_aspect"]
        self.screen_aspect = 1.6 if sa == "auto" else float(sa)
        self.mouse_px = float(rc["mouse_px_per_screen"])
        # Movement units for one screen width / height, for the relative outputs.
        if self.rel_output == "trackball":
            self.rel_counts = (float(rc["trackball_counts"][0]), float(rc["trackball_counts"][1]))
            # Equal hand movement = equal pixels on both axes: use the screen's own shape.
            self.screen_aspect = self.rel_counts[0] / max(self.rel_counts[1], 1e-6)
        else:  # mouse: pixels; the screen height is the width / aspect ratio
            self.rel_counts = (self.mouse_px, self.mouse_px / max(self.screen_aspect, 1e-6))
        # Relative outputs can't know where the real pointer is, so they keep a virtual
        # cursor (sum of their own movements, unbounded): enough to measure and undo
        # movement between the two pinches of a double click.
        self._vcur = [0.0, 0.0]
        cam = cfg["camera"]
        # h / w of the camera frames. The configured size is only what was asked for;
        # app.py calls set_frame_size() with the size the camera really delivers.
        self.frame_aspect = float(cam["height"]) / max(float(cam["width"]), 1.0)
        self._prev_t = 0.0
        self.last_gain = 0.0
        # Relative mode keeps its own cursor (screen fractions, 0..1) and sends it to the
        # absolute (tablet) device, so GNOME's mouse acceleration never stacks on ours.
        self._cursor = [0.5, 0.5]
        self._palm: float | None = None  # smoothed palm length, in camera-frame widths
        self.freeze_s = float(cc["click_freeze_ms"]) / 1000.0

        dc = cfg["double_click"]
        self.dc_enabled = bool(dc["enabled"])
        self.dc_window = float(dc["window_ms"]) / 1000.0
        self.dc_max_move = float(dc["max_move"])
        os_ms = dc["os_time_ms"]
        self.dc_os_time = (float(os_ms) if isinstance(os_ms, (int, float)) else 400.0) / 1000.0
        self._click_press_t: float | None = None    # last left press
        self._click_release_t: float | None = None  # last left release
        self._click_pos: tuple[float, float] | None = None  # cursor at last press
        self._last_was_double = False

        # Hi-res wheel units per frame height of hand movement. Both axes are measured in
        # frame heights, so the same hand distance scrolls the same amount either way.
        self.scroll_units = float(sc["notches_per_height"]) * HI_RES_PER_NOTCH
        # natural (phone): hand up moves the content up = wheel down; hand right moves
        # the content right = wheel left. traditional: the other way round.
        self.scroll_sign = -1.0 if sc["direction"] == "natural" else 1.0
        self.scroll_horizontal = bool(sc["horizontal"])
        self.scroll_axis_lock = bool(sc["axis_lock"])
        fl = sc["fling"]
        self.fling_enabled = bool(fl["enabled"])
        self.fling_min_speed = float(fl["min_speed"])
        self.fling_tau = float(fl["time_constant_s"])
        self.fling_boost = float(fl["boost"])  # glide-only speed multiplier
        self.boost_ramp = max(float(fl["boost_ramp_s"]), 1e-3)
        stk = fl["stack"]
        self.stack_enabled = bool(stk["enabled"])
        self.stack_window = float(stk["window_s"])
        self.stack_factor = float(stk["factor"])
        self.stack_settle_s = float(stk["settle_ms"]) / 1000.0
        self.max_glide = float(fl["max_notches_per_s"]) * HI_RES_PER_NOTCH
        # Scroll speeds are (horizontal, vertical) vectors in hi-res wheel units per
        # second, signed as sent: + = wheel right / wheel up.
        self._carry: Vec = ZERO      # caught glide speed carried into the next fling
        self._caught_v: Vec = ZERO   # glide speed when it was last caught by a touch
        self._caught_t = -1e9
        self._settle_s = 0.0         # settle time for the current scroll touch
        self._fling_v0: Vec = ZERO   # release speed, before boost
        self._fling_age = 0.0        # seconds since release
        self._last_t: float | None = None
        self._fling_v: Vec = ZERO    # current glide speed
        self._fling_t = 0.0
        self._fling_rem: Vec = ZERO
        self._scroll_hist: deque[tuple[float, float, float]] = deque(maxlen=15)  # t, x, y
        self._scroll_axis: str | None = None  # "x", "y", "both"; None = not decided yet
        self._scroll_lifted = False  # finger lifted: ignore hand movement until it touches again
        self._lift_glided = False
        self.scroll_deadzone = float(sc["deadzone"])
        self.scroll_settle_s = float(sc["settle_ms"]) / 1000.0
        self._mode_since = 0.0

        self.fist_hold = float(cfg["pause"]["fist_hold_s"])
        self.lost_limit = int(cfg["safety"]["lost_hand_release_frames"])

        self.mode = Mode.IDLE
        self.paused = bool(cfg["pause"]["start_paused"])
        self._lost = 0
        self._fist_start: float | None = None
        self._fist_fired = False
        self._freeze_until = 0.0
        self._last_sent: tuple[float, float] | None = None
        self._prev_mapped = None
        self._rel_rem = [0.0, 0.0]
        self._scroll_prev: Vec | None = None  # baseline point (frame heights)
        self._scroll_rem: Vec = ZERO  # fractions of a hi-res unit not sent yet
        self._last_action = ""
        self._events: list[str] = []
        self._actions: list[str] = []
        self._new_touch()  # sets the scroll axis from scroll.horizontal / axis_lock

    # -- public ---------------------------------------------------------------
    def set_frame_size(self, width: int, height: int) -> None:
        """The size of the camera frames, which can differ from camera.width/height
        when the camera doesn't offer that size. Relative movement converts vertical
        hand movement with it, so a wrong value makes up/down too fast or too slow."""
        if width <= 0 or height <= 0:
            return
        aspect = float(height) / float(width)
        if abs(aspect - self.frame_aspect) > 1e-9:
            self.frame_aspect = aspect
            self.rel_filter.reset()     # old positions were in the old units
            self._prev_mapped = None

    def tick(self) -> None:
        """Time-based housekeeping; also call this when no camera frame arrived."""
        if self.touchpad is not None:
            self.touchpad.tick()

    def stalled(self, t: float) -> Status:
        """The camera has sent no frames for a while (call this when a read times out).

        Without frames, update() never runs, so nothing would ever let go: a drag would
        keep the left button down, a Ctrl+wheel zoom would keep Ctrl held for every
        key typed, and a touchpad pinch would keep its fingers down. So release
        everything now, stop any glide, and start fresh when frames come back."""
        self._events = []
        self._actions = []
        self._now = max(t, self._now)
        self.tick()
        self._stop_fling()
        self.scroller.release()  # lift the scroll pad's fingers too
        self.pose.reset()
        self.zoom_debounce.reset()
        self._fist_start, self._fist_fired = None, False
        if self.mode != Mode.IDLE or self.pointer.any_down():
            self._enter(Mode.IDLE)
            self._act("camera stalled: released")
        return self._status(Status(paused=self.paused))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._stop_fling()
        self._enter(Mode.IDLE)
        self._act("paused" if self.paused else "resumed")

    def update(self, feats: HandFeatures | None, anchor: tuple[float, float] | None,
               t: float, palm: float | None = None,
               zoom: tuple[bool, float] | None = None, rebase: bool = False) -> Status:
        """palm: palm length (wrist to middle knuckle) in camera-frame widths. Relative
        mode measures hand movement in palm lengths, so it feels the same at any
        distance from the camera.
        zoom: (both hands pointing, index-tip spread in palm lengths) when two hands are
        visible, else None.
        rebase: the tracked hand switched (or jumped impossibly far) this frame: don't
        turn the jump into cursor or scroll movement, and disarm pinches.
        t: when the frame was captured (app.py passes the camera's timestamp). Time
        never runs backwards here: a frame captured just before a stalled() call is
        given that call's time."""
        t = max(t, self._now)
        st = Status(paused=self.paused)
        self._events = []
        self._actions = []
        prev_t, self._last_t = self._last_t, t
        self._now = t
        self.tick()
        # Catch a glide on the very first frame of the peace sign, like touching the glass.
        # Its speed is remembered: a quick swipe the same way adds it to the next fling.
        # A glide only frames old (a misread frame mid-drag) still stops, but isn't
        # remembered, so it can't stack.
        if self.gliding and feats is not None and feats.pose == TWO and not self.paused:
            real = self._fling_age >= MIN_CATCH_AGE_S
            self._caught_v, self._caught_t = (self._fling_v if real else ZERO), t
            self._stop_fling()
        self._apply_fling(t)

        if feats is None or anchor is None:
            if self.mode == Mode.ZOOM:
                self._enter(Mode.IDLE)  # never leave Ctrl held without hands in view
                self._act("zoom end")
            self.zoom_debounce.reset()
            if self.mode == Mode.SCROLL and not self._scroll_lifted:
                self._lift(t, prev_t)  # hand left the view mid-swipe: same as lifting the finger
            self._lost += 1
            if self._lost >= self.lost_limit and (self.mode != Mode.IDLE or self.pointer.any_down()):
                self._enter(Mode.IDLE)
                self._act("hand lost: released")
            self._fist_start, self._fist_fired = None, False
            return self._status(st)

        self._lost = 0
        if rebase:
            self._rebase()
        st.hand = True
        st.raw_pose = feats.pose
        # A pinch bends the index finger toward the thumb, and once it's bent about
        # halfway the hand reads as a fist. While the fingertips are closed together
        # and the index still reaches forward (a real fist curls it into the palm),
        # it's a pinch: don't let it switch to IDLE (dropping the click) or count
        # towards pausing.
        raw = feats.pose
        if raw == FIST and self._pinch_posture(feats):
            raw = NONE
        pose = self.pose.update(raw)
        st.pose = pose
        st.pinch_index, st.pinch_middle = feats.pinch_index, feats.pinch_middle

        # Fist hold -> toggle pause (works while paused too).
        if pose == FIST and not self.pointer.is_down("left"):
            if self._fist_start is None:
                self._fist_start = t
            held = t - self._fist_start
            # Progress bar only while counting; hidden once the toggle has fired.
            st.fist_progress = 0.0 if self._fist_fired else min(held / self.fist_hold, 1.0)
            if held >= self.fist_hold and not self._fist_fired:
                self._fist_fired = True
                self.toggle_pause()
        else:
            self._fist_start, self._fist_fired = None, False

        if self.paused:
            self.left.reset()
            self.right.reset()
            if self.mode == Mode.ZOOM:
                self._enter(Mode.IDLE)
            return self._status(st)

        # Two-hand zoom takes priority over every one-hand gesture.
        if self.zoom_enabled:
            # Never start a zoom in the middle of a drag: it would drop what's being dragged.
            dragging = self.pointer.is_down("left") and self.mode != Mode.ZOOM
            both = zoom is not None and zoom[0] and not dragging
            st.zoom_hands = both
            zooming = self.zoom_debounce.update("zoom" if both else NONE) == "zoom"
            # A frame where the tracker misses the second hand (zoom is None) is a
            # glitch like any other: the debouncer decides when the zoom ends, so one
            # missed frame doesn't lift the fingers (or let go of Ctrl) mid-pinch.
            if zooming and (zoom is not None or self.mode == Mode.ZOOM):
                if self.mode != Mode.ZOOM:
                    self._enter(Mode.ZOOM)
                    self._mode_since = t
                    self._act("zoom")
                self._zoom_status_blocked = ""
                if zoom is not None:
                    st.zoom_spread = zoom[1]
                    self._zoom(zoom[1], t)
                st.zoom_blocked = self._zoom_status_blocked
                st.zoom_blocked_state = self._zoom_blocked_state if st.zoom_blocked else ""
                return self._status(st)
            if self.mode == Mode.ZOOM:
                self._enter(Mode.IDLE)
                self._mode_since = t
                self._act("zoom end")
                # Wait for a clean one-hand pose before clicking or moving again.
                self.pose.reset()
                return self._status(st)

        # Pinches (always tracked so hysteresis state stays consistent).
        # A new pinch may only start while the hand faces the camera; an ongoing one
        # (e.g. a drag) is not cut off when the hand turns.
        facing_ok = feats.facing >= self.min_facing
        st.facing, st.pinch_blocked = feats.facing, not facing_ok
        # Left: the index finger must reach forward (not curled into a fist) when the
        # pinch starts. Right: no reach check, because tapping the thumb onto a bent
        # middle finger is a natural right-click; arming already stops a thumb that
        # merely rests on the finger from firing.
        _, l_start, l_end = self.left.update(
            feats.pinch_index, facing_ok, feats.reach_index >= self.min_reach)
        _, r_start, _ = self.right.update(
            feats.pinch_middle, facing_ok and not self.left.active,
            feats.reach_middle >= self.right_min_reach)
        st.left_armed, st.right_armed = self.left.armed, self.right.armed

        # Mode selection.
        # A pinch confirmed this frame counts as a drag, so it can't lose a race with a
        # pose change (e.g. a tight pinch briefly reading as a fist).
        dragging = self.pointer.is_down("left") or (self.mode == Mode.MOVE and l_start)
        if dragging:
            target = Mode.MOVE
        elif pose == POINT:
            target = Mode.MOVE
        elif pose == TWO:
            target = Mode.SCROLL
        elif pose == NONE:
            target = self.mode
        else:  # palm, fist
            target = Mode.IDLE
        if target != self.mode:
            self._enter(target)
            self._mode_since = t

        if self.mode == Mode.MOVE:
            if l_start:
                self._left_press(t)
            if l_end and self.pointer.is_down("left"):
                self.pointer.release("left")
                # After a double click, the next pinch starts a fresh sequence.
                self._click_release_t = None if self._last_was_double else t
                self._act("left up")
            if r_start and not self.pointer.is_down("left"):
                self._stop_fling()
                self.pointer.click("right")
                self._freeze_until = t + self.freeze_s
                self._act("right click")
            # While the pose is changing away from "point" (e.g. opening the palm to
            # stop), hold the cursor so the hand's own movement doesn't drag it off target.
            hold = feats.pose not in (POINT, NONE) and not self.pointer.is_down("left")
            self._move_cursor(anchor, t, st, hold=hold, palm=palm)
        elif self.mode == Mode.SCROLL:
            # Leaving the scroll pose = lifting the finger off the glass. The glide starts
            # on the very first frame out of the pose, at the drag speed, so drag and glide
            # are one continuous motion. The peace sign coming back = a new touch: it
            # catches the glide and the drag picks up from where the hand is. (A single
            # misread frame therefore shows up as one frame of glide at drag speed.)
            if self._scroll_lifted:
                if feats.pose == TWO:
                    glided = self._lift_glided
                    self._stop_fling()
                    self._scroll_lifted = False
                    self._scroll_hist.clear()
                    if glided:
                        self._new_touch()  # the glide moved the page: new baseline
                    # else keep the old baseline, so movement during a misread frame
                    # is caught up now instead of lost
                    self._scroll(anchor, t)
            elif feats.pose != TWO and not (t - self._mode_since < self._settle_s):
                self._lift(t, prev_t)
            else:
                # Only scroll on frames that really show the scroll pose.
                settling = t - self._mode_since < self._settle_s or feats.pose != TWO
                self._scroll(anchor, t, settling=settling)

        # Fingertips closed but no click: say why (preview, and once per pinch with -v).
        st.left_block = self._left_block(feats, pose, facing_ok)
        if feats.pinch_index > self.left.release_t:
            self._block_frames, self._block_said = 0, False
        elif st.left_block:
            self._block_frames += 1
            if self._block_frames >= self.left.confirm and not self._block_said:
                self._block_said = True
                self._act(f"left click blocked: {st.left_block}")
        return self._status(st)

    # -- internals ------------------------------------------------------------
    def _pinch_posture(self, feats: HandFeatures) -> bool:
        """Thumb and index tips together with the index reaching forward: a pinch."""
        return (feats.pinch_index < self.left.release_t
                and feats.reach_index >= self.min_reach)

    def _left_block(self, feats: HandFeatures, pose: str, facing_ok: bool) -> str:
        """Why fingertips that are closed together aren't clicking ("" if they are,
        or are about to: the pinch must hold for pinch.confirm_frames)."""
        if feats.pinch_index >= self.left.start or self.pointer.is_down("left"):
            return ""  # fingers apart, or clicking (a pinch outside MOVE never clicks)
        reach_low = feats.reach_index < self.min_reach
        if self.mode != Mode.MOVE:
            return (f"not pointing (pose: {pose})"
                    + (f", index bent into a fist (reach {feats.reach_index:.2f} < "
                       f"{self.min_reach:.2f})" if reach_low else ""))
        if not facing_ok:
            return f"hand turned (facing {feats.facing:.2f} < {self.min_facing:.2f})"
        if reach_low:
            return (f"index bent into a fist (reach {feats.reach_index:.2f} < "
                    f"{self.min_reach:.2f})")
        if not self.left.armed:
            return "open thumb and index first"
        return ""

    def _enter(self, mode: Mode) -> None:
        if self.mode == Mode.MOVE and mode != Mode.MOVE:
            self.pointer.release_all()
        if self.mode == Mode.ZOOM and mode != Mode.ZOOM:
            if self.keys is not None:
                self.keys.release_all()
            if self.touchpad is not None:
                self.touchpad.end()  # lift the virtual fingers
            self._tp_ref = None
        if mode == Mode.ZOOM:
            self.pointer.release_all()
            self._stop_fling()
            self.zoom_filter.reset()
            self._zoom_prev = None
            self._zoom_acc = 0.0
        if mode == Mode.SCROLL:
            self._stop_fling()  # touching the glass again stops a fling
            # Right after catching a glide, track the hand almost at once, so a quick
            # follow-up flick isn't lost in the settle period.
            stacking = self._stack_ready(self._last_t if self._last_t is not None else 0.0)
            self._settle_s = self.stack_settle_s if stacking else self.scroll_settle_s
        self._scroll_lifted = False
        self._scroll_hist.clear()
        self.mode = mode
        self.cursor_filter.reset()
        self.rel_filter.reset()
        self.scroll_filter.reset()
        self._prev_mapped = None
        self._last_sent = None
        self._rel_rem = [0.0, 0.0]
        self._new_touch()
        self.left.reset()
        self.right.reset()

    def _new_touch(self) -> None:
        """Start a scroll touch afresh: new baseline, and the axis decided again."""
        self._scroll_prev = None
        self._scroll_rem = ZERO
        if not self.scroll_horizontal:
            self._scroll_axis = "y"
        elif not self.scroll_axis_lock:
            self._scroll_axis = "both"
        else:
            self._scroll_axis = None

    @property
    def gliding(self) -> bool:
        return self._fling_v != ZERO

    def _rebase(self) -> None:
        """Forget motion history, as if the hand had just appeared, without changing
        mode. A drag in progress keeps its button (and its pinch) so it isn't dropped."""
        self.cursor_filter.reset()
        self.rel_filter.reset()
        self.scroll_filter.reset()
        self._prev_mapped = None
        self._last_sent = None
        self._rel_rem = [0.0, 0.0]
        self._new_touch()
        self._scroll_hist.clear()
        if not self.pointer.is_down("left"):
            self.left.reset()
        self.right.reset()
        self._act("hand switch", quiet=True)

    def _move_rel_screen(self, dx: float, dy: float) -> None:
        """Move by dx screen widths and dy screen heights with relative events."""
        px = dx * self.rel_counts[0] + self._rel_rem[0]
        py = dy * self.rel_counts[1] + self._rel_rem[1]
        ix, iy = int(px), int(py)
        self._rel_rem = [px - ix, py - iy]
        self._vcur[0] += dx
        self._vcur[1] += dy
        self.pointer.move_rel(ix, iy)

    def _cursor_pos(self) -> tuple[float, float] | None:
        """Cursor position as a screen fraction: real for the absolute outputs, virtual
        (relative to an arbitrary origin) for the relative ones."""
        if self.abs_mode:
            return self._last_sent
        if self.rel_output == "tablet":
            return (self._cursor[0], self._cursor[1])
        return (self._vcur[0], self._vcur[1])

    def _place(self, pos: tuple[float, float]) -> None:
        """Put the cursor at a screen fraction (for relative outputs: undo the movement
        since the virtual cursor was at pos)."""
        if self.abs_mode:
            self.pointer.move_abs(pos[0] * self.abs_scale[0], pos[1] * self.abs_scale[1])
            self._last_sent = pos
        elif self.rel_output == "tablet":
            self.pointer.move_abs(pos[0] * self.abs_scale[0], pos[1] * self.abs_scale[1])
            self._cursor = [pos[0], pos[1]]
        else:
            self._rel_rem = [0.0, 0.0]
            self._move_rel_screen(pos[0] - self._vcur[0], pos[1] - self._vcur[1])

    def _left_press(self, t: float) -> None:
        """Start a left press. A second pinch soon after the previous click, near the
        same spot, becomes a guaranteed double click:
          * the cursor snaps back to where the first click happened, and
          * if the OS double-click time has already run out since the first press,
            an extra quick click is sent first, so the OS sees two presses in time.
        """
        self._stop_fling()
        pos = self._cursor_pos()
        double = (
            self.dc_enabled
            and self._click_release_t is not None
            and t - self._click_release_t <= self.dc_window
        )
        if double and pos is not None and self._click_pos is not None:
            moved = max(abs(pos[0] - self._click_pos[0]), abs(pos[1] - self._click_pos[1]))
            double = moved <= self.dc_max_move
        if double:
            if self._click_pos is not None and pos is not None:
                self._place(self._click_pos)
            if self._click_press_t is None or t - self._click_press_t > self.dc_os_time:
                self.pointer.click("left")
            self.pointer.press("left")
            self._act("double click")
            self._last_was_double = True
        else:
            self.pointer.press("left")
            self._click_pos = pos
            self._last_was_double = False
            self._act("left down")
        self._click_press_t = t
        self._freeze_until = t + self.freeze_s

    def _map(self, anchor: tuple[float, float]) -> tuple[float, float]:
        x0, x1, y0, y1 = self.region
        nx = (anchor[0] - x0) / max(x1 - x0, 1e-6)
        ny = (anchor[1] - y0) / max(y1 - y0, 1e-6)
        return min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)

    def _move_cursor(self, anchor: tuple[float, float], t: float, st: Status,
                     hold: bool = False, palm: float | None = None) -> None:
        if self.abs_mode:
            self._move_absolute(anchor, t, st, hold)
        else:
            self._move_relative(anchor, t, st, hold, palm)

    def _move_absolute(self, anchor, t, st: Status, hold: bool) -> None:
        fx, fy = self.cursor_filter(self._map(anchor), t)
        if hold or t < self._freeze_until:
            return
        if self._last_sent is not None:
            if max(abs(fx - self._last_sent[0]), abs(fy - self._last_sent[1])) < self.deadzone:
                return
        # abs_scale (from --calibrate) shrinks the output range so the region's
        # far edges land exactly on the screen's right/bottom edges.
        self.pointer.move_abs(fx * self.abs_scale[0], fy * self.abs_scale[1])
        self._last_sent = (float(fx), float(fy))

    def _rel_gain(self, speed: float) -> float:
        """Screen widths of cursor travel per palm length of hand travel, rising
        smoothly with hand speed (palm lengths per second): slow = precise,
        fast = cross the screen with little arm movement."""
        lo, hi = self.rel_speed_slow, self.rel_speed_fast
        u = min(max((speed - lo) / max(hi - lo, 1e-6), 0.0), 1.0)
        u = u * u * (3.0 - 2.0 * u)  # smoothstep
        return self.rel_gain_slow + (self.rel_gain_fast - self.rel_gain_slow) * u

    def _move_relative(self, anchor, t, st: Status, hold: bool, palm: float | None) -> None:
        if palm is not None and palm > 1e-4:
            self._palm = palm if self._palm is None else 0.85 * self._palm + 0.15 * palm
        palm_len = self._palm or 0.16  # typical value if no palm size was given

        # Isotropic units: both axes in camera-frame widths.
        pos = (anchor[0], anchor[1] * self.frame_aspect)
        fx, fy = self.rel_filter(pos, t)
        prev, prev_t = self._prev_mapped, self._prev_t
        self._prev_mapped, self._prev_t = (fx, fy), t
        if prev is None or hold or t < self._freeze_until or t <= prev_t:
            return
        dxp = (fx - prev[0]) / palm_len  # hand movement in palm lengths
        dyp = (fy - prev[1]) / palm_len
        speed = (dxp * dxp + dyp * dyp) ** 0.5 / (t - prev_t)
        if speed < self.rel_min_speed:
            # Tracking jitter while the hand is still: don't let it creep the cursor.
            self._rel_rem = [0.0, 0.0]
            return
        gain = self._rel_gain(speed)
        self.last_gain = gain
        dx = dxp * gain                       # screen widths
        dy = dyp * gain * self.screen_aspect  # screen heights (same distance in pixels)

        if self.rel_output in ("mouse", "trackball"):
            # Relative events: the cursor moves from wherever the real pointer is.
            self._move_rel_screen(dx, dy)
            return

        cx = min(max(self._cursor[0] + dx, 0.0), 1.0)
        cy = min(max(self._cursor[1] + dy, 0.0), 1.0)
        self._cursor = [cx, cy]
        self.pointer.move_abs(cx * self.abs_scale[0], cy * self.abs_scale[1])

    def _scroll_point(self, anchor: tuple[float, float]) -> Vec:
        """The anchor in frame heights on both axes (x is in frame widths)."""
        return anchor[0] / max(self.frame_aspect, 1e-6), anchor[1]

    def _mask(self, v: Vec, axis: str | None) -> Vec:
        """Keep only the components the touch scrolls along."""
        if axis == "x":
            return v[0], 0.0
        if axis == "y":
            return 0.0, v[1]
        return v

    def _send_scroll(self, units: Vec) -> None:
        ux, uy = int(units[0]), int(units[1])
        if ux or uy:
            self.scroller.scroll(ux, uy)
        if uy:
            self._act("scroll up" if uy > 0 else "scroll down", quiet=True)
        if ux:
            self._act("scroll right" if ux > 0 else "scroll left", quiet=True)

    def _scroll(self, anchor: tuple[float, float], t: float, settling: bool = False) -> None:
        fx, fy = (float(v) for v in self.scroll_filter(self._scroll_point(anchor), t))
        if not settling:
            self._scroll_hist.append((t, fx, fy))
        if self._scroll_prev is None or settling:
            # Just switched pose: the hand is still moving into position, so only
            # track the baseline for a moment instead of scrolling.
            self._scroll_prev = (fx, fy)
            self._scroll_rem = ZERO
            return
        px, py = self._scroll_prev
        d = (fx - px, py - fy)  # hand moving right / up -> positive
        if self._scroll_axis is None:
            # Axis lock: decide which way this swipe scrolls once the hand has clearly
            # moved; the baseline stays put, so that first bit is sent right after.
            if _norm(d) < AXIS_LOCK_DIST:
                return
            self._scroll_axis = "x" if abs(d[0]) > abs(d[1]) else "y"
            self._act("scroll sideways" if self._scroll_axis == "x" else "scroll up/down",
                      quiet=True)
        d = self._mask(d, self._scroll_axis)
        if _norm(d) < self.scroll_deadzone:
            return
        self._scroll_prev = (fx, fy)
        # Carry the fraction of a unit to the next step instead of rounding it away,
        # so the page moves exactly as far as the hand did, however slowly.
        k = self.scroll_units * self.scroll_sign
        amount = (d[0] * k + self._scroll_rem[0], d[1] * k + self._scroll_rem[1])
        units = (float(int(amount[0])), float(int(amount[1])))
        self._scroll_rem = (amount[0] - units[0], amount[1] - units[1])
        self._send_scroll(units)

    def _lift(self, t: float, prev_t: float | None = None) -> None:
        """The finger left the glass. If the hand was still moving fast, keep scrolling
        from exactly that speed and let it decay (momentum), like a phone. The glide's
        first step is sent in this same frame, so there is no pause between drag and glide."""
        self._scroll_lifted = True
        self._lift_glided = False
        hist = [h for h in self._scroll_hist if h[0] >= t - 0.25]
        self._scroll_hist.clear()
        if not self.fling_enabled or len(hist) < 2:
            return
        t_end, x_end, y_end = hist[-1]
        t0, x0, y0 = next((h for h in hist if h[0] >= t_end - 0.12), hist[0])
        if t_end - t0 < 0.03:
            t0, x0, y0 = hist[0]
        if t_end <= t0:
            return
        # Hand speed in frame heights per second: right / up = positive.
        v = ((x_end - x0) / (t_end - t0), (y0 - y_end) / (t_end - t0))
        axis = self._scroll_axis
        if axis is None:  # a flick too quick to have locked yet: its own direction decides
            axis = "x" if abs(v[0]) > abs(v[1]) else "y"
        v = self._mask(v, axis)
        stack_ok = self._stack_ready(t)
        caught, self._caught_v = self._caught_v, ZERO  # a catch is used at most once
        if _norm(v) < self.fling_min_speed:
            return
        k = self.scroll_units * self.scroll_sign
        self._fling_v0 = (v[0] * k, v[1] * k)
        # Stacking: a quick swipe the same way as the glide it just caught adds that
        # glide's speed, so repeated flicks keep accelerating (up to max_notches_per_s).
        self._carry = ZERO
        if stack_ok and caught[0] * self._fling_v0[0] + caught[1] * self._fling_v0[1] > 0:
            self._carry = self._mask((caught[0] * self.stack_factor,
                                      caught[1] * self.stack_factor), axis)
            self._act("fling (stacked)")
        self._fling_v = self._fling_v0
        self._fling_age = 0.0
        self._fling_rem = ZERO
        # Continue from the previous frame's time, so this frame already moves the page.
        self._fling_t = prev_t if prev_t is not None and prev_t < t else t - 1 / 30
        self._lift_glided = True
        if self._carry == ZERO:
            self._act("fling")
        self._apply_fling(t)

    def _stack_ready(self, t: float) -> bool:
        """A glide was caught recently enough for the next fling to stack on it."""
        return (self.stack_enabled and self._caught_v != ZERO
                and t - self._caught_t <= self.stack_window)

    def _glide_speed(self, age: float) -> Vec:
        """Glide speed at a given age. Starts at exactly the release speed and eases into
        the boost along an S-curve (flat at the start, so the first frames match the drag),
        while everything decays with time_constant_s, on each axis:
            v(age) = v0 * (1 + k * (1 - e^(-age/ramp))^2) * e^(-age/tau)
        k is chosen so the total glide distance is exactly boost x the unboosted glide.
        The speed is capped at max_notches_per_s, keeping its direction."""
        tau, rmp = self.fling_tau, self.boost_ramp
        # integral of (1 - e^(-a/r))^2 * e^(-a/tau) over a = 0..inf
        d = tau - 2 * rmp * tau / (rmp + tau) + rmp * tau / (2 * tau + rmp)
        k = (self.fling_boost - 1.0) * tau / d
        ease = (1.0 - math.exp(-age / rmp)) ** 2
        decay = math.exp(-age / tau)
        out = []
        for v0, carry in zip(self._fling_v0, self._carry, strict=True):
            # A stacked glide's carried speed eases in the same way (scaled so it adds
            # its full glide distance), keeping the release continuous with the drag.
            v = (v0 * (1.0 + k * ease) + carry * tau / d * ease) * decay
            if v * v0 < 0:
                # boost well below 1 makes 1 + k * ease negative: the glide has stopped,
                # it must never turn round and scroll the page back.
                v = 0.0
            out.append(v)
        n = _norm((out[0], out[1]))
        if n > self.max_glide:
            return out[0] * self.max_glide / n, out[1] * self.max_glide / n
        return out[0], out[1]

    def _apply_fling(self, t: float) -> None:
        if not self.gliding:
            return
        dt = t - self._fling_t
        if dt <= 0:
            return
        self._fling_t = t
        # Midpoint speed over this frame: accurate for both the ramp and the decay.
        v_mid = self._glide_speed(self._fling_age + dt / 2)
        self._fling_age += dt
        amount = (v_mid[0] * dt + self._fling_rem[0], v_mid[1] * dt + self._fling_rem[1])
        units = (float(int(amount[0])), float(int(amount[1])))
        self._fling_rem = (amount[0] - units[0], amount[1] - units[1])
        ux, uy = int(units[0]), int(units[1])
        if ux or uy:
            self.scroller.scroll(ux, uy)
        self._fling_v = self._glide_speed(self._fling_age)
        if _norm(self._fling_v) < HI_RES_PER_NOTCH / 2:  # under half a notch per second
            self._stop_fling()

    def _stop_fling(self) -> None:
        self._fling_v = ZERO
        self._fling_v0 = ZERO
        self._carry = ZERO
        self._fling_rem = ZERO

    def _zoom(self, spread: float, t: float) -> None:
        """Turn changes in hand spread into zoom steps. One step (one wheel notch, or one
        Ctrl+= / Ctrl+- tap) per notch_ratio change in spread, measured on a log scale
        so zooming feels the same whether the hands start close or far apart."""
        x = float(self.zoom_filter([math.log(max(spread, 1e-3))], t)[0])
        if self._zoom_prev is None or t - self._mode_since < self.zoom_settle_s:
            self._zoom_prev = x  # hands still settling into the pose
            return
        if self.zoom_method == "touchpad":
            self._zoom_touchpad(x, t)
            return
        dx = x - self._zoom_prev
        if abs(dx) < self.zoom_deadzone:
            if (self.keys.is_down("ctrl") and self.zoom_method == "ctrl_scroll"
                    and t - self._zoom_last_step > self.zoom_ctrl_idle_s):
                self.keys.release("ctrl")  # hands held still: let go of Ctrl
            return
        self._zoom_prev = x
        steps = dx / self.zoom_step * self.zoom_sign  # + = zoom in
        self._zoom_last_step = t
        if self.zoom_method == "ctrl_scroll":
            units = int(round(steps * HI_RES_PER_NOTCH))
            if units:
                self.keys.press("ctrl")
                self.pointer.scroll(units)  # wheel up with Ctrl = zoom in
                self._act("zoom in" if units > 0 else "zoom out", quiet=True)
        else:  # keys: whole steps only
            self._zoom_acc += steps
            while abs(self._zoom_acc) >= 1.0:
                key = "equal" if self._zoom_acc > 0 else "minus"
                self.keys.press("ctrl")
                self.keys.tap(key)
                self.keys.release("ctrl")
                self._zoom_acc -= 1.0 if self._zoom_acc > 0 else -1.0
                self._act("zoom in" if key == "equal" else "zoom out", quiet=True)

    def _zoom_touchpad(self, x: float, t: float) -> None:
        """True pinch: two virtual fingers whose distance follows the hands' spread.
        Spreading the hands by a factor f moves the fingers apart by f ** gain, and apps
        zoom by the fingers' distance ratio, just like on a real touchpad. When the
        fingers reach the touchpad's edge zone (or get too close), they lift and land
        again at the start distance, the way you'd re-grip on a real touchpad."""
        tp = self.touchpad
        if self.tp_settings is not None and not self.tp_settings.usable:
            if tp.is_down:
                tp.end()
            self._tp_ref = None
            self._zoom_status_blocked = self.tp_settings.message()
            self._zoom_blocked_state = self.tp_settings.state
            if not self._blocked_said:
                self._blocked_said = True
                self._blocked_new = True
                self._act("zoom unavailable: touchpad is off")
            return
        self._blocked_said = False
        if self._tp_ref is None:
            # New pinch (or the previous one resuming before its fingers lifted).
            self._tp_base = tp.begin(self.tp_start_mm)
            self._tp_ref = x
            return
        k = self.tp_gain * self.zoom_sign
        sep = self._tp_base * math.exp((x - self._tp_ref) * k)
        if (sep > tp.max_sep or sep < tp.min_sep) and tp.contact_age >= tpm.MIN_CONTACT_S:
            # Re-grip. First use the room left: move the fingers right to the edge.
            # The spread beyond it isn't dropped either: the new grip starts as if
            # it had already zoomed that much, so the next frame continues the zoom.
            # (Re-gripping straight away lost the whole frame's zoom at each re-grip:
            # 13% of an 8x zoom done in one second.)
            edge = min(max(sep, tp.min_sep), tp.max_sep)
            tp.move(edge)
            excess = (x - self._tp_ref) - math.log(edge / self._tp_base) / k
            tp.end()                       # lift and land again at the start
            self._tp_base = tp.begin(self.tp_start_mm)
            self._tp_ref = x - excess
            self._act("zoom in" if sep > edge else "zoom out", quiet=True)
            return
        if abs(sep - tp.separation) >= 0.05:
            zoom_in = sep > tp.separation
            tp.move(sep)
            self._act("zoom in" if zoom_in else "zoom out", quiet=True)

    def _act(self, text: str, quiet: bool = False) -> None:
        self._last_action = text
        self._actions.append(text)
        if not quiet:
            self._events.append(text)

    def _status(self, st: Status) -> Status:
        # Every update() / stalled() ends here: send this frame's scroll pad event.
        self.scroller.flush(self._now)
        st.paused = self.paused
        st.mode = self.mode
        st.left_down = self.pointer.is_down("left")
        st.last_action = self._last_action
        st.events = self._events
        st.actions = self._actions
        st.zoom_blocked_new, self._blocked_new = self._blocked_new, False
        if self.mode == Mode.SCROLL:
            st.scroll_axis = {"x": "horizontal", "y": "vertical", "both": "both",
                              None: ""}[self._scroll_axis]
        return st
