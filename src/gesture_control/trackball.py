"""Relative cursor output that starts from wherever the real pointer is.

The default "tablet" output moves the cursor to absolute positions the app keeps track
of itself. Wayland doesn't let apps read the real pointer position, so after you use a
physical mouse or touchpad, the next gesture puts the cursor back where gestures left it.

Relative movement doesn't have that problem, but GNOME adds its own mouse acceleration
to relative devices, on top of the app's. The way around it (from mutter's source):
mutter applies the *trackball* acceleration setting
(org.gnome.desktop.peripherals.trackball accel-profile) to devices that udev tags
ID_INPUT_TRACKBALL. So the app creates a separate virtual pointer named
"gesture-control trackball", a one-time udev rule tags it as a trackball, and the
trackball profile is set to flat. Your real mouse and touchpad keep their settings.

With flat acceleration, libinput scales movement by a constant (1 + mouse speed), and
mutter also scales by the display scale. Rather than guess those, --calibrate-trackball
measures how many movement units cross the screen.
"""

from __future__ import annotations

import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEVICE_NAME = "gesture-control trackball"
RULE_PATH = Path("/etc/udev/rules.d/99-gesture-control.rules")
RULE = ('ACTION=="add|change", SUBSYSTEM=="input", KERNEL=="event*", '
        f'ATTRS{{name}}=="{DEVICE_NAME}", ENV{{ID_INPUT_TRACKBALL}}="1"')
SETUP_COMMANDS = f"""\
  echo '{RULE}' | sudo tee {RULE_PATH}
  sudo udevadm control --reload-rules
  gsettings set org.gnome.desktop.peripherals.trackball accel-profile flat
  uv run gesture-control --calibrate-trackball"""


def _gsettings(schema: str, key: str) -> str | None:
    if not shutil.which("gsettings"):
        return None
    try:
        out = subprocess.run(["gsettings", "get", schema, key], capture_output=True,
                             text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip().strip("'") if out.returncode == 0 else None


def flat_profile() -> bool:
    return _gsettings("org.gnome.desktop.peripherals.trackball", "accel-profile") == "flat"


def mouse_speed() -> float | None:
    """GNOME's mouse speed (-1..1). It also applies to trackballs (libinput factor 1+speed)."""
    value = _gsettings("org.gnome.desktop.peripherals.mouse", "speed")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def rule_installed() -> bool:
    try:
        return DEVICE_NAME in RULE_PATH.read_text()
    except OSError:
        return False


def device_tagged(path: str | None, timeout: float = 1.5) -> bool:
    """Has udev tagged the device at `path` (/dev/input/eventN) as a trackball?"""
    if not path or not shutil.which("udevadm"):
        return False
    end = time.monotonic() + timeout
    while True:
        try:
            # Query by sysfs path: works without read access to /dev/input/eventN.
            sys_path = f"/sys/class/input/{Path(path).name}"
            out = subprocess.run(["udevadm", "info", "--query=property", f"--path={sys_path}"],
                                 capture_output=True, text=True, timeout=2, check=False)
            if "ID_INPUT_TRACKBALL=1" in out.stdout:
                return True
        except (OSError, subprocess.SubprocessError):
            return False
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def missing_steps(cfg: dict, check_counts: bool = True) -> list[str]:
    """Setup steps still needed before the trackball output can be used."""
    missing = []
    if not rule_installed():
        missing.append(f"the udev rule {RULE_PATH} isn't installed")
    if not flat_profile():
        missing.append("GNOME's trackball acceleration isn't set to flat")
    counts = cfg["cursor"]["relative"]["trackball_counts"]
    if check_counts and not (counts and min(counts) > 0):
        missing.append("the trackball output isn't calibrated yet (--calibrate-trackball)")
    return missing


# -- calibration -----------------------------------------------------------------------
def _enter_pressed(timeout: float) -> bool:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        sys.stdin.readline()
        return True
    return False


def _push(ptr, dx: int, dy: int, total: int) -> None:
    """Move far in one direction so the cursor pins against the screen edge."""
    for _ in range(max(1, total // 400)):
        ptr.move_rel(400 * (dx > 0) - 400 * (dx < 0), 400 * (dy > 0) - 400 * (dy < 0))
        time.sleep(0.004)
    time.sleep(0.15)


def _sweep(ptr, axis: int, sign: int, start: int, stop: int, step: int,
           interval: float) -> int | None:
    """Move step units at a time from `start` units away from the pinned edge, until
    Enter. Returns the units travelled from the edge, or None."""
    done = 0
    while done < start:  # quick jump to the starting point
        d = min(400, start - done)
        ptr.move_rel(d * sign if axis == 0 else 0, d * sign if axis == 1 else 0)
        done += d
        time.sleep(0.004)
    pos = start
    while pos <= stop:
        if _enter_pressed(interval):
            return pos
        ptr.move_rel(step * sign if axis == 0 else 0, step * sign if axis == 1 else 0)
        pos += step
    return None


def _measure(ptr, axis: int) -> int:
    big = 40000
    if axis == 0:
        label, edge = "WIDTH", "right"
        pin = lambda: _push(ptr, -1, +1, big)  # noqa: E731  bottom-left: no hot corner
        sign = +1
    else:
        label, edge = "HEIGHT", "top"
        pin = lambda: _push(ptr, +1, +1, big)  # noqa: E731  bottom-right
        sign = -1
    print(f"\n--- Screen {label.lower()} ---")
    input(f"Press Enter to start. Watch the cursor and press Enter again the moment it "
          f"touches the {edge} edge of the screen.")
    pin()
    first = _sweep(ptr, axis, sign, 0, 20000, 12, 0.025)
    if first is None:
        sys.exit("The cursor never reached the edge. Is the trackball output tagged? "
                 "Run: uv run gesture-control --calibrate-trackball again after setup.")
    input("Got it. Now a slower pass to fine-tune: press Enter to start, then Enter "
          "again the moment it touches.")
    pin()
    start = max(first - 120, 0)
    second = _sweep(ptr, axis, sign, start, first + 60, 1, 0.02)
    value = second if second is not None else first
    print(f"Screen {label.lower()}: {value} units.")
    return value


def run_calibration(cfg: dict, dry_run: bool, config_path: str | None) -> None:
    from .config import update_calibration
    from .pointer import VirtualPointer

    missing = missing_steps(cfg, check_counts=False)
    if missing and not dry_run:
        print("Trackball output setup isn't complete:")
        for m in missing:
            print(f"  - {m}")
        print("Run these once, then this calibration again:\n" + SETUP_COMMANDS)
        sys.exit(1)
    if not sys.stdin.isatty() and not dry_run:
        sys.exit("Calibration needs an interactive terminal.")
    print("Trackball calibration: measures how far the cursor moves, so gestures move it "
          "the same distance as before.\nKeep this terminal focused and don't touch the "
          "mouse. With several monitors, use your laptop screen only.")
    try:
        ptr = VirtualPointer(mode="relative", dry_run=dry_run, name=DEVICE_NAME)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput (see the setup steps in the README).")
    try:
        if not dry_run and ptr.device_path is None:
            sys.exit("Couldn't find the virtual trackball's /dev/input/event node. "
                     "Please report this.")
        if not dry_run and not device_tagged(ptr.device_path):
            sys.exit(f"The virtual trackball ({ptr.device_path}) wasn't tagged by udev. "
                     f"Check the rule file {RULE_PATH} and run: "
                     "sudo udevadm control --reload-rules")
        cx = _measure(ptr, 0)
        cy = _measure(ptr, 1)
    except (KeyboardInterrupt, EOFError):
        print("\nCalibration cancelled; nothing saved.")
        return
    finally:
        ptr.close()
    out = update_calibration({"cursor": {"relative": {
        "trackball_counts": [cx, cy], "trackball_mouse_speed": mouse_speed()}}}, config_path)
    print(f"\nSaved {cx} x {cy} units to {out}.")
    print("The cursor now follows your real pointer: start the app as usual.")
