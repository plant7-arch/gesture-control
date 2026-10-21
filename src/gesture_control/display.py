"""Screen size and scale from GNOME (org.gnome.Mutter.DisplayConfig over D-Bus).

Used for two things:
  * the trackball output's movement units per screen width/height, so it needs no
    calibration: mutter counts relative motion in logical pixels (resolution / scale,
    see meta_seat_impl_filter_relative_motion), and libinput's flat profile multiplies
    by (1 + mouse speed);
  * the screen's aspect ratio (cursor.relative.screen_aspect: auto).

No special permissions are needed: GNOME Settings uses the same call. The primary
monitor is used when there are several.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass

_DEST = "org.gnome.Mutter.DisplayConfig"
_PATH = "/org/gnome/Mutter/DisplayConfig"


@dataclass
class Display:
    connector: str
    width: int    # current mode, physical pixels (after rotation)
    height: int
    scale: float

    @property
    def logical(self) -> tuple[float, float]:
        """Size in logical pixels (what the desktop and relative motion use)."""
        return self.width / self.scale, self.height / self.scale

    @property
    def aspect(self) -> float:
        return self.width / self.height

    def describe(self) -> str:
        return f"{self.width}x{self.height} at {self.scale * 100:.0f}%"


def _run(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _make(connector: str, w: int, h: int, scale: float, transform: int) -> Display | None:
    if w <= 0 or h <= 0 or scale <= 0:
        return None
    if transform % 2 == 1:  # 90 / 270 degrees (flipped or not): sides swap
        w, h = h, w
    return Display(connector, w, h, scale)


# -- busctl --json (systemd): structured, preferred -----------------------------------
def _unvariant(v):
    return v.get("data") if isinstance(v, dict) and "data" in v and "type" in v else v


def parse_busctl(text: str) -> Display | None:
    try:
        data = json.loads(text)["data"]
        _serial, monitors, logical = data[0], data[1], data[2]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    current: dict[str, tuple[int, int]] = {}
    for mon in monitors:
        try:
            connector, modes = mon[0][0], mon[1]
        except (IndexError, TypeError):
            continue
        for mode in modes:
            props = mode[6] if len(mode) > 6 and isinstance(mode[6], dict) else {}
            if _unvariant(props.get("is-current")) is True:
                current[connector] = (int(mode[1]), int(mode[2]))
    if not logical:
        return None
    chosen = next((lm for lm in logical if len(lm) > 4 and lm[4] is True), logical[0])
    try:
        _x, _y, scale, transform, _primary, mons = chosen[:6]
        connector = mons[0][0]
    except (ValueError, IndexError, TypeError):
        return None
    if connector not in current:
        return None
    w, h = current[connector]
    return _make(connector, w, h, float(scale), int(transform))


# -- gdbus text output: fallback -------------------------------------------------------
_MON_RE = re.compile(r"\(\('([^']*)', '[^']*', '[^']*', '[^']*'\), \[")
_MODE_RE = re.compile(r"\('[^']*', (\d+), (\d+), [\d.e+-]+, [\d.e+-]+, \[[^\]]*\], \{([^}]*)\}\)")
_LOGICAL_RE = re.compile(
    r"\((-?\d+), (-?\d+), ([\d.]+), (?:uint32 )?(\d+), (true|false), \[\('([^']*)'")


def parse_gdbus(text: str) -> Display | None:
    current: dict[str, tuple[int, int]] = {}
    starts = list(_MON_RE.finditer(text))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        for mode in _MODE_RE.finditer(text, m.end(), end):
            if re.search(r"'is-current': <true>", mode.group(3)):
                current[m.group(1)] = (int(mode.group(1)), int(mode.group(2)))
                break
    logical = _LOGICAL_RE.findall(text)
    if not logical:
        return None
    chosen = next((lm for lm in logical if lm[4] == "true"), logical[0])
    connector = chosen[5]
    if connector not in current:
        return None
    w, h = current[connector]
    return _make(connector, w, h, float(chosen[2]), int(chosen[3]))


def primary_display() -> Display | None:
    """The primary monitor's current mode and scale, or None if GNOME can't be asked."""
    out = _run(["busctl", "--user", "--json=short", "call", _DEST, _PATH, _DEST,
                "GetCurrentState"])
    disp = parse_busctl(out) if out else None
    if disp is None:
        out = _run(["gdbus", "call", "--session", "--dest", _DEST, "--object-path", _PATH,
                    "--method", f"{_DEST}.GetCurrentState"])
        disp = parse_gdbus(out) if out else None
    return disp


def trackball_counts(disp: Display, mouse_speed: float | None) -> tuple[float, float]:
    """Trackball movement units for one screen width and height."""
    factor = max(0.005, 1.0 + (mouse_speed or 0.0))  # libinput's flat profile
    lw, lh = disp.logical
    return lw / factor, lh / factor
