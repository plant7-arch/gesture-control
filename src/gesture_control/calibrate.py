"""Screen-edge calibration for the absolute pointer.

GNOME can map the virtual tablet's range onto an area larger than the visible screen
(for example with fractional display scaling), so the cursor hits the right/bottom edge
before the hand reaches the far side of the region. This sweeps the cursor slowly
towards each edge; you press Enter the moment it touches, and the fraction is saved
to config/calibration.yaml as cursor.abs_scale.

Pressing late is harmless (the edge is still reachable); pressing early is not, so the
second, slower sweep refines the first.
"""

from __future__ import annotations

import select
import sys
import time

from .config import command, save_calibration
from .pointer import VirtualPointer

COARSE = (0.004, 0.03)   # (step, seconds per step)  -> about 0.13 of the range per second
FINE = (0.001, 0.03)     # about 0.03 per second


def _enter_pressed(timeout: float) -> bool:
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        sys.stdin.readline()
        return True
    return False


def _sweep(ptr: VirtualPointer, axis: int, start: float, stop: float,
           step: float, interval: float) -> float | None:
    """Move along one axis until Enter is pressed. Returns the position, or None."""
    v = start
    while v <= stop + 1e-9:
        ptr.move_abs(v, 0.5) if axis == 0 else ptr.move_abs(0.5, v)
        if _enter_pressed(interval):
            return min(v, 1.0)
        v += step
    return None


def _measure(ptr: VirtualPointer, axis: int) -> float:
    edge = "RIGHT" if axis == 0 else "BOTTOM"
    print(f"\n--- {edge} edge ---")
    input(f"Press Enter to start. Watch the cursor and press Enter again the moment "
          f"it touches the {edge.lower()} edge of the screen.")
    first = _sweep(ptr, axis, 0.3, 1.0, *COARSE)
    if first is None:
        print("Reached the end of the range without a key press: no scaling needed on this axis.")
        return 1.0

    input("Got it. Now a slower pass to fine-tune: press Enter to start, "
          "then Enter again the moment it touches.")
    start = max(first - 0.08, 0.3)
    ptr.move_abs(start, 0.5) if axis == 0 else ptr.move_abs(0.5, start)
    time.sleep(0.4)
    second = _sweep(ptr, axis, start, min(first + 0.03, 1.0), *FINE)
    value = second if second is not None else first
    print(f"{edge.capitalize()} edge at {value:.3f} of the range.")
    return value


def run_calibration(dry_run: bool, config_path: str | None) -> None:
    if not sys.stdin.isatty() and not dry_run:
        sys.exit("Calibration needs an interactive terminal.")
    print("Calibration: maps your screen edges for absolute cursor mode.\n"
          "Keep this terminal focused; the cursor moves on its own, so don't touch the mouse.")
    try:
        ptr = VirtualPointer(mode="absolute", dry_run=dry_run)
    except PermissionError:
        sys.exit("Permission denied on /dev/uinput: see README > Setup, step 3.")

    try:
        sx = _measure(ptr, 0)
        sy = _measure(ptr, 1)
        ptr.move_abs(0.5 * sx, 0.5 * sy)
    except (KeyboardInterrupt, EOFError):
        print("\nCalibration cancelled; nothing saved.")
        return
    finally:
        ptr.close()

    if dry_run:
        # The cursor never moved, so these numbers mean nothing: don't save them.
        print(f"\nDry run: would save abs_scale [{sx:.3f}, {sy:.3f}]; nothing written.")
        return
    out = save_calibration((sx, sy), config_path)
    print(f"\nSaved abs_scale [{sx:.3f}, {sy:.3f}] to {out}")
    print(f"Check it with: {command()} --pointer-test "
          "(the cursor should land exactly in all four corners).")
