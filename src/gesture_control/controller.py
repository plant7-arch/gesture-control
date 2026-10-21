"""Gesture state machine: turns per-frame hand features into pointer actions.

Modes
  IDLE   - hand visible but not controlling anything (palm, unknown pose)
  MOVE   - "point" pose: cursor follows the hand (absolute) or its movement
           (relative, touchpad-like); pinches click/drag
  SCROLL - "two" pose: vertical hand movement scrolls, phone-style by default: the
           content follows the hand, and leaving the pose mid-swipe flings it

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

from .features import FIST, NONE, POINT, TWO, HandFeatures
from .filters import OneEuroFilter
from .pointer import HI_RES_PER_NOTCH, VirtualPointer


class Mode(str, Enum):
    IDLE = "idle"
    MOVE = "move"
    SCROLL = "scroll"


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
    right_armed: bool = False
    cursor: tuple[float, float] | None = None  # normalized screen position
    fist_progress: float = 0.0
    last_action: str = ""
    events: list[str] = field(default_factory=list)


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
    def __init__(self, cfg: dict[str, Any], pointer: VirtualPointer):
        self.cfg = cfg
        self.pointer = pointer
        pc, cc, sc = cfg["pinch"], cfg["cursor"], cfg["scroll"]

        self.pose = Debouncer(cfg["pose"]["debounce_frames"])
        self.left = PinchDetector(pc["start"], pc["release"], pc["confirm_frames"])
        self.right = PinchDetector(pc["start"], pc["release"], pc["confirm_frames"])
        self.min_reach = float(pc["min_finger_reach"])
        self.right_min_reach = float(pc["right_min_reach"])
        self.min_facing = float(pc["min_facing"])

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
        self.rel_output = rc["output"]
        self.screen_aspect = float(rc["screen_aspect"])
        self.mouse_px = float(rc["mouse_px_per_screen"])
        cam = cfg["camera"]
        self.frame_aspect = float(cam["height"]) / max(float(cam["width"]), 1.0)  # h / w
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

        self.scroll_units = float(sc["notches_per_height"]) * HI_RES_PER_NOTCH
        # natural (phone): hand up moves the content up = wheel down.
        self.scroll_sign = -1.0 if sc["direction"] == "natural" else 1.0
        fl = sc["fling"]
        self.fling_enabled = bool(fl["enabled"])
        self.fling_min_speed = float(fl["min_speed"])
        self.fling_tau = float(fl["time_constant_s"])
        self.lift_frames = max(1, int(fl["lift_frames"]))
        self._fling_v = 0.0          # hi-res wheel units per second (signed)
        self._fling_t = 0.0
        self._fling_rem = 0.0
        self._scroll_hist: deque[tuple[float, float]] = deque(maxlen=15)
        self._scroll_lifted = False  # finger "lifted": ignore the hand until re-entry
        self._not_two = 0
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
        self._scroll_prev: float | None = None
        self._last_action = ""
        self._events: list[str] = []

    # -- public ---------------------------------------------------------------
    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self._stop_fling()
        self._enter(Mode.IDLE)
        self._act("paused" if self.paused else "resumed")

    def update(self, feats: HandFeatures | None, anchor: tuple[float, float] | None,
               t: float, palm: float | None = None) -> Status:
        """palm: palm length (wrist to middle knuckle) in camera-frame widths. Relative
        mode measures hand movement in palm lengths, so it feels the same at any
        distance from the camera."""
        st = Status(paused=self.paused)
        self._events = []
        # Catch a glide on the very first frame of the peace sign, like touching the glass.
        if self._fling_v and feats is not None and feats.pose == TWO and not self.paused:
            self._stop_fling()
        self._apply_fling(t)

        if feats is None or anchor is None:
            if self.mode == Mode.SCROLL and not self._scroll_lifted:
                self._lift(t)  # hand left the view mid-swipe: same as lifting the finger
            self._lost += 1
            if self._lost >= self.lost_limit and (self.mode != Mode.IDLE or self.pointer.any_down()):
                self._enter(Mode.IDLE)
                self._act("hand lost: released")
            self._fist_start, self._fist_fired = None, False
            return self._status(st)

        self._lost = 0
        st.hand = True
        st.raw_pose = feats.pose
        pose = self.pose.update(feats.pose)
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
            # Leaving the scroll pose for lift_frames frames = lifting the finger off the
            # glass: a fast swipe keeps gliding (fling). A single odd frame is ignored.
            self._not_two = 0 if feats.pose == TWO else self._not_two + 1
            if self._scroll_lifted:
                pass
            elif self._not_two >= self.lift_frames:
                self._lift(t)
            else:
                # Only scroll on frames that really show the scroll pose.
                settling = t - self._mode_since < self.scroll_settle_s or feats.pose != TWO
                self._scroll(anchor, t, settling=settling)

        return self._status(st)

    # -- internals ------------------------------------------------------------
    def _enter(self, mode: Mode) -> None:
        if self.mode == Mode.MOVE and mode != Mode.MOVE:
            self.pointer.release_all()
        if mode == Mode.SCROLL:
            self._stop_fling()  # touching the glass again stops a fling
        self._scroll_lifted = False
        self._not_two = 0
        self._scroll_hist.clear()
        self.mode = mode
        self.cursor_filter.reset()
        self.rel_filter.reset()
        self.scroll_filter.reset()
        self._prev_mapped = None
        self._last_sent = None
        self._rel_rem = [0.0, 0.0]
        self._scroll_prev = None
        self.left.reset()
        self.right.reset()

    def _cursor_pos(self) -> tuple[float, float] | None:
        """Current cursor position as a screen fraction, if this mode knows it."""
        if self.abs_mode:
            return self._last_sent
        if self.rel_output == "tablet":
            return (self._cursor[0], self._cursor[1])
        return None  # plain relative mouse events: position unknown

    def _place(self, pos: tuple[float, float]) -> None:
        """Put the cursor at a screen fraction (tablet/absolute output only)."""
        self.pointer.move_abs(pos[0] * self.abs_scale[0], pos[1] * self.abs_scale[1])
        if self.abs_mode:
            self._last_sent = pos
        else:
            self._cursor = [pos[0], pos[1]]

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
        st.cursor = (float(fx), float(fy))
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
        st.cursor = (self._cursor[0], self._cursor[1])
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

        if self.rel_output == "mouse":
            # Plain relative mouse events (GNOME's acceleration applies on top).
            px = dx * self.mouse_px + self._rel_rem[0]
            py = dy * self.mouse_px + self._rel_rem[1]
            ix, iy = int(px), int(py)
            self._rel_rem = [px - ix, py - iy]
            self.pointer.move_rel(ix, iy)
            return

        cx = min(max(self._cursor[0] + dx, 0.0), 1.0)
        cy = min(max(self._cursor[1] + dy, 0.0), 1.0)
        self._cursor = [cx, cy]
        st.cursor = (cx, cy)
        self.pointer.move_abs(cx * self.abs_scale[0], cy * self.abs_scale[1])

    def _scroll(self, anchor: tuple[float, float], t: float, settling: bool = False) -> None:
        y = float(self.scroll_filter([anchor[1]], t)[0])
        if not settling:
            self._scroll_hist.append((t, y))
        if self._scroll_prev is None or settling:
            # Just switched pose: the hand is still moving into position, so only
            # track the baseline for a moment instead of scrolling.
            self._scroll_prev = y
            return
        dy = self._scroll_prev - y  # hand moving up -> positive
        if abs(dy) < self.scroll_deadzone:
            return
        self._scroll_prev = y
        units = int(round(dy * self.scroll_units * self.scroll_sign))
        if units:
            self.pointer.scroll(units)
            self._act("scroll up" if units > 0 else "scroll down", quiet=True)

    def _lift(self, t: float) -> None:
        """The finger left the glass. If the hand was still moving fast, keep scrolling
        with that speed and let it decay (momentum), like a phone."""
        self._scroll_lifted = True
        hist = [(ht, hy) for ht, hy in self._scroll_hist if ht >= t - 0.25]
        self._scroll_hist.clear()
        if not self.fling_enabled or len(hist) < 2:
            return
        t_end, y_end = hist[-1]
        t0, y0 = next(((ht, hy) for ht, hy in hist if ht >= t_end - 0.12), hist[0])
        if t_end - t0 < 0.03:
            t0, y0 = hist[0]
        if t_end <= t0:
            return
        v = (y0 - y_end) / (t_end - t0)  # frame heights per second, hand up = positive
        if abs(v) < self.fling_min_speed:
            return
        self._fling_v = v * self.scroll_units * self.scroll_sign
        self._fling_t = t
        self._fling_rem = 0.0
        self._act("fling")

    def _apply_fling(self, t: float) -> None:
        if self._fling_v == 0.0:
            return
        dt = t - self._fling_t
        if dt <= 0:
            return
        self._fling_t = t
        amount = self._fling_v * dt + self._fling_rem
        units = int(amount)
        self._fling_rem = amount - units
        if units:
            self.pointer.scroll(units)
        self._fling_v *= math.exp(-dt / self.fling_tau)
        if abs(self._fling_v) < HI_RES_PER_NOTCH / 2:  # under half a notch per second
            self._stop_fling()

    def _stop_fling(self) -> None:
        self._fling_v = 0.0
        self._fling_rem = 0.0

    def _act(self, text: str, quiet: bool = False) -> None:
        self._last_action = text
        if not quiet:
            self._events.append(text)

    def _status(self, st: Status) -> Status:
        st.paused = self.paused
        st.mode = self.mode
        st.left_down = self.pointer.is_down("left")
        st.last_action = self._last_action
        st.events = self._events
        return st
