"""Where scrolling goes: a two-finger swipe on a virtual touchpad, or the mouse wheel.

Apps treat the two differently. With a touchpad, image viewers, maps and PDF viewers
pan, and a pinch-zoomed web page scrolls sideways; with a mouse wheel, the image
viewer zooms instead, and a pinch-zoomed page only scrolls up and down (measured on
GNOME 50 with Firefox and the image viewer). So scrolling is sent as a touchpad swipe
by default (scroll.method: touchpad), and as the mouse wheel when asked, or when
GNOME's touchpad settings would block the swipe.

The scroll engine (controller.py) works out how far to scroll in mouse-wheel units
(120 per notch, + = wheel up / wheel right). The swipe turns those into finger travel
(scroll.touchpad.mm_per_notch millimetres per notch) on a virtual touchpad:

  * Two fingers side by side, 20 mm apart, move together: libinput reads that as a
    two-finger scroll. They land in the middle of a large (1 m) pad, so they rarely
    run out of room; when they do, they stop, lift and land in the middle again.
  * They lift only after holding still for a moment: apps add their own momentum to
    a swipe that lifts while moving, and the app's own fling (controller.py) already
    does that.
  * The first move comes a frame after the fingers land, and no move is faster than
    libinput accepts: it discards sudden jumps of 20 mm in 12 ms as sensor glitches.
  * GNOME's touchpad natural-scroll setting applies to this pad too, so the fingers
    move whichever way gives the scroll direction asked for.
"""

from __future__ import annotations

import math
import time

from .pointer import HI_RES_PER_NOTCH, make_uinput
from .touchpad import record

PAD_MM = 1000.0            # size of the virtual scroll pad (square)
RES = 10                   # device units per mm
GAP_MM = 20.0              # the two fingers, side by side
MARGIN = 0.12              # keep the fingers this far (share of the size) from the edges
STILL_BEFORE_LIFT_S = 0.3  # lift only after the fingers held still this long
REGRIP_STILL_S = 0.08      # out of room mid-swipe: hold still this long, then lift
MIN_LAND_S = 0.02          # first move at least this long after landing
MAX_SPEED_MM_S = 1500.0    # fastest finger movement sent (libinput's jump limit ~1667)


class PadScroller:
    """Scrolls with two virtual fingers. Call scroll() as often as needed during a
    frame and flush() once per frame: each flush sends at most one touchpad event."""

    method = "touchpad"

    def __init__(self, mm_per_notch: float, settings=None, dry_run: bool = False,
                 settle: float = 0.5, name: str = "gesture-control scroll pad"):
        self.mm_per_unit = float(mm_per_notch) / HI_RES_PER_NOTCH
        self.settings = settings
        self.dry_run = dry_run
        lo, hi = PAD_MM * MARGIN, PAD_MM * (1 - MARGIN)
        self.limits = (lo + GAP_MM / 2, hi - GAP_MM / 2, lo, hi)  # centre x / y range
        self.centre = (PAD_MM / 2, PAD_MM / 2)
        self._pos = self.centre
        self._pending = [0.0, 0.0]   # mm still to move (pad axes: + = right / down)
        self._down = False
        self._land_t = self._last_move_t = self._last_event_t = 0.0
        self._regrip_since: float | None = None
        self._tracking_id = 0
        # The last events sent: ("down"|"move", x, y) / ("up",). Bounded (see record()).
        self.events: list[tuple] = []
        self._ui = None
        if dry_run:
            return
        from evdev import AbsInfo
        from evdev import ecodes as e

        self._e = e
        size = int(PAD_MM * RES)
        axis = AbsInfo(value=0, min=0, max=size, fuzz=0, flat=0, resolution=RES)
        caps = {
            e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_FINGER,
                       e.BTN_TOOL_DOUBLETAP, e.BTN_TOOL_TRIPLETAP],
            e.EV_ABS: [
                (e.ABS_X, axis), (e.ABS_Y, axis),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, 4, 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, axis), (e.ABS_MT_POSITION_Y, axis),
            ],
        }
        # Same kind of device as the zoom touchpad (touchpad.py), which GNOME accepts.
        self._ui = make_uinput(caps, name=name, vendor=0x1209, product=0x6766,
                               bustype=e.BUS_USB,
                               input_props=[e.INPUT_PROP_POINTER, e.INPUT_PROP_BUTTONPAD])
        time.sleep(settle)

    # -- what the controller calls -------------------------------------------------
    @property
    def natural(self) -> bool:
        return True if self.settings is None else bool(self.settings.natural)

    def scroll(self, ux: float, uy: float) -> None:
        """Add a scroll in mouse-wheel units (+ = wheel right / wheel up)."""
        # With natural scrolling the content follows the fingers: wheel up (content
        # down) = fingers down; wheel right (content left) = fingers left.
        s = 1.0 if self.natural else -1.0
        self._pending[0] -= ux * self.mm_per_unit * s
        self._pending[1] += uy * self.mm_per_unit * s

    def flush(self, now: float) -> None:
        """Send this frame's finger event (at most one)."""
        if self._regrip_since is not None:
            if now - self._regrip_since >= REGRIP_STILL_S:
                self._regrip_since = None
                self._lift()             # next flush lands in the middle again
            return
        if math.hypot(*self._pending) >= 1.0 / RES:
            if not self._down:
                self._land(now)
                return
            if now - self._land_t < MIN_LAND_S:
                return
            # Never faster than libinput accepts; the rest goes in the next frames.
            cap = MAX_SPEED_MM_S * max(now - self._last_event_t, 0.008)
            dx, dy = self._pending
            n = math.hypot(dx, dy)
            if n > cap:
                dx, dy = dx * cap / n, dy * cap / n
            x, y = self._pos[0] + dx, self._pos[1] + dy
            x0, x1, y0, y1 = self.limits
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                self._regrip_since = now  # out of room: hold still, then lift
                return
            self._move(x, y, now)
            self._pending[0] -= dx
            self._pending[1] -= dy
            self._last_move_t = now
        elif self._down and now - self._last_move_t >= STILL_BEFORE_LIFT_S:
            self._lift()

    def release(self) -> None:
        """Lift now and forget what wasn't sent (camera stall, shutdown)."""
        self._pending = [0.0, 0.0]
        self._regrip_since = None
        if self._down:
            self._lift()

    def close(self) -> None:
        self.release()
        if self._ui is not None:
            self._ui.close()
            self._ui = None

    @property
    def is_down(self) -> bool:
        return self._down

    # -- events --------------------------------------------------------------------
    def _fingers(self, x: float, y: float, new: bool) -> None:
        if self.dry_run:
            return
        e, w = self._e, self._ui.write
        pts = ((x - GAP_MM / 2, y), (x + GAP_MM / 2, y))
        for slot, (px, py) in enumerate(pts):
            w(e.EV_ABS, e.ABS_MT_SLOT, slot)
            if new:
                self._tracking_id = (self._tracking_id + 1) % 65000
                w(e.EV_ABS, e.ABS_MT_TRACKING_ID, self._tracking_id)
            w(e.EV_ABS, e.ABS_MT_POSITION_X, round(px * RES))
            w(e.EV_ABS, e.ABS_MT_POSITION_Y, round(py * RES))
        if new:
            w(e.EV_KEY, e.BTN_TOUCH, 1)
            w(e.EV_KEY, e.BTN_TOOL_DOUBLETAP, 1)
        w(e.EV_ABS, e.ABS_X, round(pts[0][0] * RES))  # single-touch emulation
        w(e.EV_ABS, e.ABS_Y, round(pts[0][1] * RES))
        self._ui.syn()

    def _land(self, now: float) -> None:
        self._pos = self.centre
        self._down = True
        self._land_t = self._last_move_t = self._last_event_t = now
        record(self.events, ("down", *self._pos))
        self._fingers(*self._pos, new=True)

    def _move(self, x: float, y: float, now: float) -> None:
        self._pos = (x, y)
        self._last_event_t = now
        record(self.events, ("move", x, y))
        self._fingers(x, y, new=False)

    def _lift(self) -> None:
        self._down = False
        record(self.events, ("up",))
        if self.dry_run:
            return
        e, w = self._e, self._ui.write
        for slot in (0, 1):
            w(e.EV_ABS, e.ABS_MT_SLOT, slot)
            w(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        w(e.EV_KEY, e.BTN_TOUCH, 0)
        w(e.EV_KEY, e.BTN_TOOL_DOUBLETAP, 0)
        self._ui.syn()


class Scroller:
    """Sends scrolling to the touchpad swipe when there is one and GNOME's settings
    allow it, otherwise to the mouse wheel. Without a pad it is just the wheel."""

    def __init__(self, pointer, pad: PadScroller | None = None, settings=None):
        self.pointer, self.pad, self.settings = pointer, pad, settings

    @property
    def method(self) -> str:
        return "touchpad" if self._use_pad() else "wheel"

    def _use_pad(self) -> bool:
        return self.pad is not None and (self.settings is None
                                         or self.settings.scroll_usable)

    def scroll(self, ux: int, uy: int) -> None:
        """Scroll by mouse-wheel units: + = wheel right / wheel up."""
        if self._use_pad():
            self.pad.scroll(ux, uy)
            return
        if self.pad is not None and self.pad.is_down:
            self.pad.release()   # settings changed mid-swipe: let go of the pad
        if uy:
            self.pointer.scroll(int(uy))
        if ux:
            self.pointer.scroll_h(int(ux))

    def flush(self, now: float) -> None:
        if self.pad is not None:
            self.pad.flush(now)

    def release(self) -> None:
        if self.pad is not None:
            self.pad.release()

    def close(self) -> None:
        if self.pad is not None:
            self.pad.close()
