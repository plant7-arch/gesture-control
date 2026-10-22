"""A virtual multi-touch touchpad for true pinch-to-zoom.

The app places two virtual fingers on this touchpad and moves them apart or together
with the user's hands. libinput sees a real two-finger pinch and GNOME passes it on to
apps through the Wayland pointer-gestures protocol, exactly like a physical touchpad.

Touchpad details that matter here (from libinput's touchpad gesture code):
  * Two touches less than 40 mm apart horizontally AND less than 7 mm apart
    vertically, 150 ms after landing, are classified as a two-finger SCROLL, and
    after 300 ms that can no longer become a pinch. So the fingers sit on a
    diagonal (like a thumb and index finger), always at least 8 mm apart
    vertically: then libinput decides purely by direction, and fingers moving apart
    or together are a pinch however long the hands rested first.
  * Contacts in the outer 8% of the width (palm) or below 85% of the height (thumb)
    may be ignored, so the fingers stay inside those lines.
  * With tap-to-click on, a quick two-finger touch is a right click, so the fingers
    always stay down for at least MIN_CONTACT_S. A lift requested sooner is scheduled
    (see tick()), never waited for, so the app doesn't freeze.
  * GNOME's touchpad settings apply to this device too (see TouchpadSettings).
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time

from .config import shown

_now = time.monotonic  # the clock (tests replace it)
MIN_CONTACT_S = 0.25  # longer than libinput's tap timeout (~180 ms)
ANGLE_DEG = 30.0      # finger diagonal: 30 degrees above horizontal
MIN_DY_MM = 8.0       # libinput's scroll box is < 7 mm vertically
START_ROOM_MM = 10.0  # zoom.touchpad.start_mm: at least this much room to zoom each way
EVENT_HISTORY = 1000  # how many sent events .events keeps (tests read them)


def record(events: list, event: tuple) -> None:
    """Add to a device's record of sent events, keeping only the last EVENT_HISTORY
    (trimmed in batches, so it stays a plain list and costs nothing per event)."""
    events.append(event)
    if len(events) > 2 * EVENT_HISTORY:
        del events[:-EVENT_HISTORY]


def safe_area(width_mm: float, height_mm: float) -> tuple[float, float, float, float]:
    """Where the fingers may go (x0, x1, y0, y1 in mm): 3 mm inside libinput's palm
    edges (8% of the width each side, 5% of the height at the top) and the thumb
    line (85% of the height)."""
    edge_x = min(8.0, width_mm * 0.08) + 3.0
    return (edge_x, width_mm - edge_x, height_mm * 0.05 + 3.0, height_mm * 0.85 - 3.0)


def separation_range(width_mm: float, height_mm: float) -> tuple[float, float]:
    """The closest and widest the two fingers can be (mm) on a pad this size."""
    x0, x1, y0, y1 = safe_area(width_mm, height_mm)
    a = math.radians(ANGLE_DEG)
    widest = min((x1 - x0) / math.cos(a), (y1 - y0) / math.sin(a))
    return MIN_DY_MM / math.sin(a), widest


def start_range(width_mm: float, height_mm: float) -> tuple[float, float]:
    """Where a pinch may start (zoom.touchpad.start_mm) so the fingers have at least
    START_ROOM_MM to move apart (zoom in) and together (zoom out) before a re-grip."""
    lo, hi = separation_range(width_mm, height_mm)
    # Rounded to 0.1 mm: 8 / sin(30 deg) is 16.000000000000004, not 16.
    return round(lo + START_ROOM_MM, 1), round(hi - START_ROOM_MM, 1)


class VirtualTouchpad:
    """Two-finger pinches on a virtual clickpad. Sizes are in millimetres."""

    def __init__(self, cfg: dict, dry_run: bool = False,
                 name: str = "gesture-control touchpad", settle: float = 0.5):
        self.width_mm = float(cfg["width_mm"])
        self.height_mm = float(cfg["height_mm"])
        self.res = 40  # units per mm
        self.dry_run = dry_run
        self._ui = None
        self._down = False
        self._down_t = 0.0
        self._tracking_id = 0
        self._sep = 0.0
        self._lift_at: float | None = None  # scheduled lift time (monotonic)
        # The last events sent: ("down"/"move"/"up", sep). Bounded, so a long session
        # doesn't keep every finger movement in memory.
        self.events: list[tuple[str, float]] = []
        if dry_run:
            return
        from evdev import AbsInfo
        from evdev import ecodes as e

        from .pointer import make_uinput

        self._e = e
        xmax, ymax = int(self.width_mm * self.res), int(self.height_mm * self.res)
        ax = AbsInfo(value=0, min=0, max=xmax, fuzz=0, flat=0, resolution=self.res)
        ay = AbsInfo(value=0, min=0, max=ymax, fuzz=0, flat=0, resolution=self.res)
        caps = {
            e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_FINGER,
                       e.BTN_TOOL_DOUBLETAP, e.BTN_TOOL_TRIPLETAP],
            e.EV_ABS: [
                (e.ABS_X, ax), (e.ABS_Y, ay),
                (e.ABS_MT_SLOT, AbsInfo(0, 0, 4, 0, 0, 0)),
                (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
                (e.ABS_MT_POSITION_X, ax), (e.ABS_MT_POSITION_Y, ay),
            ],
        }
        # USB bus: an "external" touchpad, so laptop disable-while-typing doesn't pair
        # it with the built-in keyboard.
        self._ui = make_uinput(caps, name=name, vendor=0x1209, product=0x6765,
                               bustype=e.BUS_USB,
                               input_props=[e.INPUT_PROP_POINTER, e.INPUT_PROP_BUTTONPAD])
        time.sleep(settle)  # let udev and the compositor pick up the new device

    # -- geometry -------------------------------------------------------------
    def _safe(self) -> tuple[float, float, float, float]:
        return safe_area(self.width_mm, self.height_mm)

    @property
    def min_sep(self) -> float:
        return separation_range(self.width_mm, self.height_mm)[0]

    @property
    def max_sep(self) -> float:
        return separation_range(self.width_mm, self.height_mm)[1]

    def finger_mm(self, sep: float) -> tuple[tuple[float, float], tuple[float, float]]:
        """Finger positions in mm: lower-left ("thumb") and upper-right, around the
        centre of the safe area. y grows downwards, as on the device."""
        x0, x1, y0, y1 = self._safe()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        a = math.radians(ANGLE_DEG)
        hx, hy = sep / 2 * math.cos(a), sep / 2 * math.sin(a)
        return (cx - hx, cy + hy), (cx + hx, cy - hy)

    def _positions(self, sep: float) -> tuple[tuple[int, int], tuple[int, int]]:
        to = lambda mm: int(round(mm * self.res))  # noqa: E731
        (ax, ay), (bx, by) = self.finger_mm(sep)
        return (to(ax), to(ay)), (to(bx), to(by))

    # -- events ---------------------------------------------------------------
    def _w(self, etype, code, value) -> None:
        self._ui.write(etype, code, value)

    def _write_fingers(self, sep: float, new_contact: bool) -> None:
        e = self._e
        (x0, y0), (x1, y1) = self._positions(sep)
        for slot, (x, y) in enumerate(((x0, y0), (x1, y1))):
            self._w(e.EV_ABS, e.ABS_MT_SLOT, slot)
            if new_contact:
                self._tracking_id = (self._tracking_id + 1) % 65000
                self._w(e.EV_ABS, e.ABS_MT_TRACKING_ID, self._tracking_id)
            self._w(e.EV_ABS, e.ABS_MT_POSITION_X, x)
            self._w(e.EV_ABS, e.ABS_MT_POSITION_Y, y)
        if new_contact:
            self._w(e.EV_KEY, e.BTN_TOUCH, 1)
            self._w(e.EV_KEY, e.BTN_TOOL_DOUBLETAP, 1)
        # single-touch emulation: first finger
        self._w(e.EV_ABS, e.ABS_X, x0)
        self._w(e.EV_ABS, e.ABS_Y, y0)
        self._ui.syn()

    def begin(self, sep: float) -> float:
        """Put two fingers down, sep mm apart. Returns the separation the pinch starts
        from: if a lift was only scheduled (the previous pinch ended moments ago), the
        same contact simply continues from where the fingers are."""
        if self._down:
            self._lift_at = None
            return self._sep
        sep = min(max(sep, self.min_sep), self.max_sep)
        self._down, self._down_t, self._sep = True, _now(), sep
        record(self.events, ("down", sep))
        if self.dry_run:
            print(f"[touchpad] 2 fingers down, {sep:.1f} mm apart")
            return sep
        self._write_fingers(sep, new_contact=True)
        return sep

    def move(self, sep: float) -> None:
        """Move the two fingers to sep mm apart (symmetric about the centre)."""
        if not self._down or self._lift_at is not None:
            return
        sep = min(max(sep, self.min_sep), self.max_sep)
        if abs(sep - self._sep) < 1.0 / self.res:
            return
        self._sep = sep
        record(self.events, ("move", sep))
        if self.dry_run:
            print(f"[touchpad] fingers {sep:.1f} mm apart")
            return
        self._write_fingers(sep, new_contact=False)

    @property
    def contact_age(self) -> float:
        return _now() - self._down_t if self._down else 0.0

    @property
    def lift_pending(self) -> bool:
        return self._lift_at is not None

    def end(self, wait: bool = False) -> None:
        """Lift both fingers. A contact younger than MIN_CONTACT_S could be read as a
        tap (right click), so its lift is scheduled for later (tick() performs it)
        unless wait=True, which sleeps instead (only for shutdown and tests)."""
        if not self._down:
            return
        remaining = MIN_CONTACT_S - (_now() - self._down_t)
        if remaining > 0:
            if not wait:
                self._lift_at = self._down_t + MIN_CONTACT_S
                return
            time.sleep(remaining)
        self._lift_now()

    def tick(self) -> None:
        """Perform a scheduled lift once its time has come. Call this every frame."""
        if self._lift_at is not None and _now() >= self._lift_at:
            self._lift_now()

    def _lift_now(self) -> None:
        self._lift_at = None
        self._down = False
        record(self.events, ("up", self._sep))
        if self.dry_run:
            print("[touchpad] fingers up")
            return
        e = self._e
        for slot in (0, 1):
            self._w(e.EV_ABS, e.ABS_MT_SLOT, slot)
            self._w(e.EV_ABS, e.ABS_MT_TRACKING_ID, -1)
        self._w(e.EV_KEY, e.BTN_TOUCH, 0)
        self._w(e.EV_KEY, e.BTN_TOOL_DOUBLETAP, 0)
        self._ui.syn()

    @property
    def is_down(self) -> bool:
        return self._down

    @property
    def separation(self) -> float:
        return self._sep

    def close(self) -> None:
        self.end(wait=True)
        if self._ui is not None:
            self._ui.close()
            self._ui = None


class TouchpadSettings:
    """Watches GNOME's touchpad settings (Settings > Mouse & Touchpad), which also apply
    to the virtual touchpads (pinch zoom and scrolling). Polled in the background
    every few seconds.

    state: "enabled", "disabled", "disabled-on-external-mouse", or "unknown"
    natural: natural scrolling (content follows the fingers); GNOME's default is on
    two_finger: two-finger scrolling (off means edge scrolling)
    """

    SCHEMA = "org.gnome.desktop.peripherals.touchpad"
    KEY = (SCHEMA, "send-events")

    def __init__(self, poll_s: float = 3.0, start: bool = True):
        self.poll_s = poll_s
        self.state = self.read()
        self._read_scrolling()
        self._stop = threading.Event()
        self._thread = None
        if start and self.state != "unknown":
            self._thread = threading.Thread(target=self._run, name="touchpad-settings",
                                            daemon=True)
            self._thread.start()

    @classmethod
    def read(cls) -> str:
        if not shutil.which("gsettings"):
            return "unknown"
        try:
            out = subprocess.run(["gsettings", "get", *cls.KEY], capture_output=True,
                                 text=True, timeout=2, check=False)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        value = out.stdout.strip().strip("'")
        return value if value in ("enabled", "disabled", "disabled-on-external-mouse") \
            else "unknown"

    @classmethod
    def read_bool(cls, key: str, default: bool) -> bool:
        """A true/false touchpad setting; the default if it can't be read."""
        if not shutil.which("gsettings"):
            return default
        try:
            out = subprocess.run(["gsettings", "get", cls.SCHEMA, key], capture_output=True,
                                 text=True, timeout=2, check=False)
        except (OSError, subprocess.SubprocessError):
            return default
        value = out.stdout.strip()
        return {"true": True, "false": False}.get(value, default)

    def _read_scrolling(self) -> None:
        self.natural = self.read_bool("natural-scroll", True)
        self.two_finger = self.read_bool("two-finger-scrolling-enabled", True)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.state = self.read()
            self._read_scrolling()

    @property
    def usable(self) -> bool:
        return self.state not in ("disabled", "disabled-on-external-mouse")

    @property
    def scroll_usable(self) -> bool:
        """Can the virtual touchpad scroll? It needs the touchpad on and two-finger
        scrolling (not edge scrolling) turned on."""
        return self.usable and self.two_finger

    def scroll_message(self) -> str:
        """Why scrolling uses the mouse wheel instead of the touchpad ("" if it doesn't)."""
        if self.scroll_usable:
            return ""
        if not self.two_finger:
            why = ("two-finger scrolling is turned off (Settings > Mouse & Touchpad > "
                   "Touchpad > Scroll Method)")
        elif self.state == "disabled-on-external-mouse":
            why = ("the touchpad is set to turn off while a mouse is connected, and this "
                   "app's virtual pointer counts as a mouse (to change it: gsettings set "
                   "org.gnome.desktop.peripherals.touchpad send-events enabled)")
        else:
            why = "the touchpad is turned off in Settings > Mouse & Touchpad"
        return (f"Scrolling uses the mouse wheel instead of a touchpad swipe: {why}. "
                "Some apps then zoom instead of panning (image viewer), and pinch-zoomed "
                "pages don't scroll sideways.")

    def message(self) -> str:
        if self.state == "disabled":
            return ("Zoom is off: the touchpad is turned off in Settings > Mouse & Touchpad. "
                    "Turn it on to zoom with the two-hand pinch, or set zoom.method: "
                    f"ctrl_scroll in {shown('local.yaml')}.")
        if self.state == "disabled-on-external-mouse":
            return ("Zoom is off, and so is your laptop touchpad while this app runs: the "
                    "touchpad is set to turn off while a mouse is connected, and this app's "
                    "virtual pointer counts as a mouse. Allow the touchpad with a mouse "
                    "connected (gsettings set org.gnome.desktop.peripherals.touchpad "
                    "send-events enabled), or set zoom.method: ctrl_scroll in "
                    f"{shown('local.yaml')}.")
        return ""

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def mouse_note() -> str:
        return ("Note: your laptop touchpad is off while this app runs. It's set to turn off "
                "while a mouse is connected, and this app's virtual pointer counts as a "
                "mouse. To keep it on: gsettings set org.gnome.desktop.peripherals.touchpad "
                "send-events enabled")
