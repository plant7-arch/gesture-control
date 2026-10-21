"""A virtual multi-touch touchpad for true pinch-to-zoom.

The app places two virtual fingers on this touchpad and moves them apart or together
with the user's hands. libinput sees a real two-finger pinch and GNOME passes it on to
apps through the Wayland pointer-gestures protocol, exactly like a physical touchpad.

Touchpad details that matter here:
  * libinput may ignore contacts near the edges (palm) or bottom (thumb), so the
    fingers stay in the central area.
  * With tap-to-click on, a quick two-finger touch is a right click, so the fingers
    always stay down for at least MIN_CONTACT_S.
  * GNOME's touchpad settings apply to this device too (see TouchpadSettings).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

MIN_CONTACT_S = 0.25  # longer than libinput's tap timeout (~180 ms)


class VirtualTouchpad:
    """Two-finger pinches on a virtual clickpad. Sizes are in millimetres."""

    def __init__(self, cfg: dict, dry_run: bool = False,
                 name: str = "gesture-control touchpad"):
        self.width_mm = float(cfg["width_mm"])
        self.height_mm = float(cfg["height_mm"])
        self.res = 40  # units per mm
        self.dry_run = dry_run
        self._ui = None
        self._down = False
        self._down_t = 0.0
        self._tracking_id = 0
        self._sep = 0.0
        self.events: list[tuple[str, float]] = []  # dry-run record: ("down"/"move"/"up", sep)
        if dry_run:
            return
        from evdev import AbsInfo, UInput
        from evdev import ecodes as e

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
        self._ui = UInput(caps, name=name, vendor=0x1209, product=0x6765,
                          bustype=e.BUS_USB,
                          input_props=[e.INPUT_PROP_POINTER, e.INPUT_PROP_BUTTONPAD])
        time.sleep(0.5)  # let udev and the compositor pick up the new device

    # -- geometry -------------------------------------------------------------
    @property
    def min_sep(self) -> float:
        return 12.0

    @property
    def max_sep(self) -> float:
        return self.width_mm * 0.76  # fingers stay clear of the edge (palm) zones

    def _positions(self, sep: float) -> tuple[tuple[int, int], tuple[int, int]]:
        cx, cy = self.width_mm / 2, self.height_mm * 0.45  # above the bottom (thumb) zone
        half = sep / 2
        to = lambda mm: int(round(mm * self.res))  # noqa: E731
        return (to(cx - half), to(cy)), (to(cx + half), to(cy))

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

    def begin(self, sep: float) -> None:
        """Put two fingers down, sep mm apart."""
        if self._down:
            return
        sep = min(max(sep, self.min_sep), self.max_sep)
        self._down, self._down_t, self._sep = True, time.monotonic(), sep
        self.events.append(("down", sep))
        if self.dry_run:
            print(f"[touchpad] 2 fingers down, {sep:.1f} mm apart")
            return
        self._write_fingers(sep, new_contact=True)

    def move(self, sep: float) -> None:
        """Move the two fingers to sep mm apart (symmetric about the centre)."""
        if not self._down:
            return
        sep = min(max(sep, self.min_sep), self.max_sep)
        if abs(sep - self._sep) < 1.0 / self.res:
            return
        self._sep = sep
        self.events.append(("move", sep))
        if self.dry_run:
            print(f"[touchpad] fingers {sep:.1f} mm apart")
            return
        self._write_fingers(sep, new_contact=False)

    def end(self) -> None:
        """Lift both fingers (after at least MIN_CONTACT_S, so it is never a tap)."""
        if not self._down:
            return
        wait = MIN_CONTACT_S - (time.monotonic() - self._down_t)
        if wait > 0:
            time.sleep(wait)
        self._down = False
        self.events.append(("up", self._sep))
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
        self.end()
        if self._ui is not None:
            self._ui.close()
            self._ui = None


class TouchpadSettings:
    """Watches GNOME's touchpad on/off setting (Settings > Mouse & Touchpad), which also
    applies to the virtual touchpad. Polled in the background every few seconds.

    state: "enabled", "disabled", "disabled-on-external-mouse", or "unknown"
    """

    KEY = ("org.gnome.desktop.peripherals.touchpad", "send-events")

    def __init__(self, poll_s: float = 3.0, start: bool = True):
        self.poll_s = poll_s
        self.state = self.read()
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

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.state = self.read()

    @property
    def usable(self) -> bool:
        return self.state not in ("disabled", "disabled-on-external-mouse")

    def message(self) -> str:
        if self.state == "disabled":
            return ("Zoom is off: the touchpad is turned off in Settings > Mouse & Touchpad. "
                    "Turn it on to zoom with the two-hand pinch, or set zoom.method: "
                    "ctrl_scroll in config/local.yaml.")
        if self.state == "disabled-on-external-mouse":
            return ("Zoom is off: the touchpad is set to turn off while a mouse is connected, "
                    "and this app's virtual pointer counts as a mouse. Allow the touchpad "
                    "with a mouse connected (gsettings set org.gnome.desktop.peripherals."
                    "touchpad send-events enabled), or set zoom.method: ctrl_scroll in "
                    "config/local.yaml.")
        return ""

    def stop(self) -> None:
        self._stop.set()
