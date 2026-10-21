"""Virtual mouse via the kernel's uinput: works on Wayland, X11 and the console."""

from __future__ import annotations

import glob
import os
import re
import time

ABS_MAX = 32767
HI_RES_PER_NOTCH = 120

BUTTONS = ("left", "right", "middle")


class VirtualPointer:
    """A virtual pointer device.

    mode="absolute": an absolute pointer (like a VM's USB tablet); hand position
                     maps directly to screen position.
    mode="relative": a normal mouse; hand movement maps to cursor movement.
    dry_run=True:    prints events instead of creating a device (no /dev/uinput needed).
    """

    def __init__(self, mode: str = "absolute", dry_run: bool = False,
                 name: str = "gesture-control pointer"):
        self.mode = mode
        self.name = name
        self.dry_run = dry_run
        self._down: set[str] = set()
        self._wheel_acc = 0
        self._last_abs: tuple[int, int] | None = None
        self._ui = None
        if dry_run:
            return

        from evdev import AbsInfo, UInput
        from evdev import ecodes as e

        self._e = e
        self._btn = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}
        self._hi_res = hasattr(e, "REL_WHEEL_HI_RES")
        rel = [e.REL_WHEEL] + ([e.REL_WHEEL_HI_RES] if self._hi_res else [])
        caps: dict = {e.EV_KEY: list(self._btn.values())}
        if mode == "absolute":
            axis = AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)
            caps[e.EV_ABS] = [(e.ABS_X, axis), (e.ABS_Y, axis)]
        else:
            rel = [e.REL_X, e.REL_Y] + rel
        caps[e.EV_REL] = rel
        self._ui = UInput(caps, name=name, vendor=0x1209, product=0x6763)
        time.sleep(0.5)  # let the compositor pick up the new device

    @property
    def device_path(self) -> str | None:
        """The /dev/input/eventN node of the virtual device (None in dry-run).

        python-evdev only fills in UInput.device when it can open the event node, and
        normal users can't (only root and the input group can read /dev/input/event*).
        So find the node through sysfs, which needs no permissions."""
        if self._ui is None:
            return None
        dev = getattr(self._ui, "device", None)
        if getattr(dev, "path", None):
            return dev.path
        node = _event_node_from_fd(self._ui.fd) or _event_node_by_name(self.name)
        return f"/dev/input/{node}" if node else None

    # -- motion -------------------------------------------------------------
    def move_abs(self, nx: float, ny: float) -> None:
        """Move to a normalized screen position (0..1, 0..1)."""
        x = int(round(min(max(nx, 0.0), 1.0) * ABS_MAX))
        y = int(round(min(max(ny, 0.0), 1.0) * ABS_MAX))
        if (x, y) == self._last_abs:
            return
        self._last_abs = (x, y)
        if self.dry_run:
            print(f"[pointer] abs {nx:.3f},{ny:.3f}")
            return
        e = self._e
        self._ui.write(e.EV_ABS, e.ABS_X, x)
        self._ui.write(e.EV_ABS, e.ABS_Y, y)
        self._ui.syn()

    def move_rel(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        if self.dry_run:
            print(f"[pointer] rel {dx:+d},{dy:+d}")
            return
        e = self._e
        if dx:
            self._ui.write(e.EV_REL, e.REL_X, int(dx))
        if dy:
            self._ui.write(e.EV_REL, e.REL_Y, int(dy))
        self._ui.syn()

    # -- buttons ------------------------------------------------------------
    def press(self, button: str) -> None:
        if button in self._down:
            return
        self._down.add(button)
        self._button(button, 1)

    def release(self, button: str) -> None:
        if button not in self._down:
            return
        self._down.discard(button)
        self._button(button, 0)

    def click(self, button: str) -> None:
        self.press(button)
        time.sleep(0.01)
        self.release(button)

    def is_down(self, button: str) -> bool:
        return button in self._down

    def any_down(self) -> bool:
        return bool(self._down)

    def _button(self, button: str, value: int) -> None:
        if self.dry_run:
            print(f"[pointer] {button} {'down' if value else 'up'}")
            return
        e = self._e
        self._ui.write(e.EV_KEY, self._btn[button], value)
        self._ui.syn()

    # -- wheel --------------------------------------------------------------
    def scroll(self, hi_res_units: int) -> None:
        """Scroll by hi-res units (120 = one wheel notch). Positive = up."""
        hi_res_units = int(hi_res_units)
        if hi_res_units == 0:
            return
        self._wheel_acc += hi_res_units
        notches = int(self._wheel_acc / HI_RES_PER_NOTCH)  # truncates toward zero
        self._wheel_acc -= notches * HI_RES_PER_NOTCH
        if self.dry_run:
            print(f"[pointer] scroll {hi_res_units:+d} (notches {notches:+d})")
            return
        e = self._e
        if self._hi_res:
            self._ui.write(e.EV_REL, e.REL_WHEEL_HI_RES, hi_res_units)
        if notches:
            self._ui.write(e.EV_REL, e.REL_WHEEL, notches)
        if self._hi_res or notches:
            self._ui.syn()

    # -- lifecycle ----------------------------------------------------------
    def release_all(self) -> None:
        for b in list(self._down):
            self.release(b)
        self._wheel_acc = 0

    def close(self) -> None:
        self.release_all()
        if self._ui is not None:
            self._ui.close()
            self._ui = None


def _event_node_from_fd(fd: int) -> str | None:
    """eventN of a uinput device, from its sysfs entry (UI_GET_SYSNAME)."""
    try:
        from evdev import _uinput

        sysname = _uinput.get_sysname(fd)
        for entry in os.listdir(f"/sys/devices/virtual/input/{sysname}"):
            if re.fullmatch(r"event[0-9]+", entry):
                return entry
    except (ImportError, AttributeError, OSError):
        pass
    return None


def _event_node_by_name(name: str) -> str | None:
    """Newest eventN whose device has this name (sysfs, no permissions needed)."""
    best = None
    for path in glob.glob("/sys/class/input/event*/device/name"):
        try:
            with open(path) as f:
                if f.read().strip() != name:
                    continue
        except OSError:
            continue
        num = int(re.search(r"event([0-9]+)", path).group(1))
        best = num if best is None else max(best, num)
    return None if best is None else f"event{best}"


class VirtualKeys:
    """A tiny virtual keyboard for zoom: Ctrl (held while zooming with the wheel) and
    Ctrl+= / Ctrl+- taps. Kept separate from the pointer so the pointer device's type
    doesn't change; GNOME combines modifier keys across devices."""

    KEYS = ("ctrl", "equal", "minus")

    def __init__(self, dry_run: bool = False, name: str = "gesture-control keys"):
        self.dry_run = dry_run
        self._down: set[str] = set()
        self._ui = None
        if dry_run:
            return
        from evdev import UInput
        from evdev import ecodes as e

        self._e = e
        self._code = {"ctrl": e.KEY_LEFTCTRL, "equal": e.KEY_EQUAL, "minus": e.KEY_MINUS}
        self._ui = UInput({e.EV_KEY: list(self._code.values())}, name=name,
                          vendor=0x1209, product=0x6764)
        time.sleep(0.3)

    def press(self, key: str) -> None:
        if key in self._down:
            return
        self._down.add(key)
        self._send(key, 1)

    def release(self, key: str) -> None:
        if key not in self._down:
            return
        self._down.discard(key)
        self._send(key, 0)

    def is_down(self, key: str) -> bool:
        return key in self._down

    def tap(self, key: str) -> None:
        self.press(key)
        time.sleep(0.005)
        self.release(key)

    def _send(self, key: str, value: int) -> None:
        if self.dry_run:
            print(f"[keys] {key} {'down' if value else 'up'}")
            return
        self._ui.write(self._e.EV_KEY, self._code[key], value)
        self._ui.syn()

    def release_all(self) -> None:
        for k in list(self._down):
            self.release(k)

    def close(self) -> None:
        self.release_all()
        if self._ui is not None:
            self._ui.close()
            self._ui = None
