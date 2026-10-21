"""Shared test helpers: recording virtual devices, synthetic hands, a frame driver.

Nothing here needs a camera, /dev/uinput or a desktop: the virtual devices run in
dry-run mode and record what they would have sent.
"""

from __future__ import annotations

import copy

import numpy as np
import pytest

from gesture_control import config as config_mod
from gesture_control import touchpad as tpm
from gesture_control.config import DEFAULTS, _deep_merge
from gesture_control.controller import GestureController
from gesture_control.features import (
    FIST,
    NONE,
    PALM,
    POINT,
    TWO,
    HandFeatures,
)
from gesture_control.pointer import VirtualKeys, VirtualPointer
from gesture_control.touchpad import VirtualTouchpad

FPS = 30.0
DT = 1.0 / FPS


@pytest.fixture(autouse=True)
def _no_gsettings(monkeypatch):
    """Config loading asks GNOME for the double-click time; tests use a fixed value."""
    monkeypatch.setattr(config_mod, "gnome_double_click_ms", lambda default=400: 400)


# -- recording devices ----------------------------------------------------------

class RecordingPointer(VirtualPointer):
    """Dry-run pointer that records events in .log instead of printing them.
    Button state (press/release/is_down) is the real VirtualPointer logic."""

    def __init__(self, mode: str = "absolute"):
        super().__init__(mode=mode, dry_run=True, settle=0.0)
        self.log: list[tuple] = []

    def move_abs(self, nx: float, ny: float) -> None:
        self.log.append(("abs", float(nx), float(ny)))

    def move_rel(self, dx: int, dy: int) -> None:
        if dx or dy:
            self.log.append(("rel", int(dx), int(dy)))

    def _button(self, button: str, value: int) -> None:
        self.log.append(("btn", button, value))

    def scroll(self, hi_res_units: int) -> None:
        if hi_res_units:
            self.log.append(("scroll", int(hi_res_units)))

    # convenience views
    def of(self, kind: str) -> list[tuple]:
        return [e for e in self.log if e[0] == kind]

    def scroll_total(self) -> int:
        return sum(e[1] for e in self.of("scroll"))

    def last_abs(self) -> tuple[float, float] | None:
        a = self.of("abs")
        return (a[-1][1], a[-1][2]) if a else None


class RecordingKeys(VirtualKeys):
    def __init__(self):
        super().__init__(dry_run=True, settle=0.0)
        self.log: list[tuple[str, int]] = []

    def _send(self, key: str, value: int) -> None:
        self.log.append((key, value))


class QuietTouchpad(VirtualTouchpad):
    """Dry-run touchpad; its .events list already records down / move / up."""

    def __init__(self, cfg):
        super().__init__(cfg, dry_run=True, settle=0.0)


# -- synthetic hands -------------------------------------------------------------

# A right hand facing the camera, fingers pointing up (image y grows downwards),
# in an isotropic space with the palm (wrist -> middle knuckle) about 1 unit long.
_MCP = {"index": (-0.30, -1.00), "middle": (0.0, -1.05),
        "ring": (0.25, -1.00), "pinky": (0.48, -0.90)}
_BASE = {"index": 5, "middle": 9, "ring": 13, "pinky": 17}


def make_hand(up=("index",), pinch_to: str | None = None) -> np.ndarray:
    """21 landmarks: fingers in `up` extended, the others curled into the palm.
    pinch_to="index" / "middle" puts the thumb tip on that fingertip."""
    pts = np.zeros((21, 3))
    pts[1:5] = [(-0.35, -0.25, 0), (-0.60, -0.45, 0), (-0.75, -0.65, 0), (-0.85, -0.80, 0)]
    for name, (x, y) in _MCP.items():
        b = _BASE[name]
        mcp = np.array([x, y, 0.0])
        pip = mcp + (0, -0.45, 0)
        if name in up:
            dip, tip = pip + (0, -0.30, 0), pip + (0, -0.55, 0)
        else:  # folded down towards the palm
            dip, tip = pip + (0, 0.20, -0.25), mcp + (0, 0.30, -0.25)  # tip in the palm
        pts[b:b + 4] = [mcp, pip, dip, tip]
    if pinch_to:
        pts[4] = pts[{"index": 8, "middle": 12}[pinch_to]]
    return pts


def rot_y(pts: np.ndarray, deg: float) -> np.ndarray:
    a = np.radians(deg)
    r = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    return pts @ r.T


# -- controller driver -----------------------------------------------------------

def feats(pose: str = POINT, pinch_index: float = 1.0, pinch_middle: float = 1.0,
          facing: float = 1.0, reach_index: float = 1.8,
          reach_middle: float = 1.8) -> HandFeatures:
    """HandFeatures for a given pose (the controller only reads these fields)."""
    up = {POINT: {"index"}, TWO: {"index", "middle"},
          PALM: {"index", "middle", "ring", "pinky"}, FIST: set(), NONE: {"index", "pinky"}}[pose]
    names = ("index", "middle", "ring", "pinky")
    return HandFeatures(
        finger_ratio={n: 1.4 if n in up else 0.7 for n in names},
        extended={n: n in up for n in names},
        curled={n: n not in up for n in names},
        pinch_index=pinch_index, pinch_middle=pinch_middle,
        reach_index=reach_index, reach_middle=reach_middle,
        palm_size=1.0, facing=facing, pose=pose,
    )


def make_config(**overrides) -> dict:
    """DEFAULTS plus nested overrides, e.g. make_config(scroll={"direction": "traditional"})."""
    cfg = _deep_merge(copy.deepcopy(DEFAULTS), overrides)
    cfg["double_click"]["os_time_ms"] = 400
    return cfg




class Rig:
    """Feeds frames to a GestureController at 30 fps on a fake clock."""

    def __init__(self, cfg: dict | None = None, keys=False, touchpad=False):
        self.cfg = cfg or make_config()
        cc = self.cfg["cursor"]
        rel_out = cc["relative"]["output"]
        dev = "relative" if cc["mode"] == "relative" and rel_out in ("mouse", "trackball") \
            else "absolute"
        self.ptr = RecordingPointer(dev)
        self.keys = RecordingKeys() if keys else None
        self.tp = QuietTouchpad(self.cfg["zoom"]["touchpad"]) if touchpad else None
        self.ctl = GestureController(self.cfg, self.ptr, self.keys, self.tp)
        self.t = 100.0
        self.events: list[str] = []
        self.anchor = (0.5, 0.5)
        self.last = None

    def step(self, f: HandFeatures | None = None, anchor=None, palm: float = 0.16,
             zoom=None, rebase: bool = False, **kw):
        """One frame. f=None with no kwargs means 'no hand'. kwargs build feats()."""
        if f is None and kw:
            f = feats(**kw)
        if anchor is not None:
            self.anchor = anchor
        self.t += DT
        st = self.ctl.update(f, self.anchor if f is not None else None, self.t, palm,
                             zoom, rebase=rebase)
        self.events += st.events
        self.last = st
        return st

    def hold(self, n: int, **kw):
        st = None
        for _ in range(n):
            st = self.step(**kw)
        return st

    def no_hand(self, n: int = 1):
        st = None
        for _ in range(n):
            st = self.step(None)
        return st

    def move(self, n: int, dx: float = 0.0, dy: float = 0.0, **kw):
        """n frames, moving the anchor by (dx, dy) each frame."""
        st = None
        for _ in range(n):
            self.anchor = (self.anchor[0] + dx, self.anchor[1] + dy)
            st = self.step(**kw)
        return st

    # gestures built from frames
    def point(self, n: int = 5, **kw):
        return self.hold(n, pose=POINT, **kw)

    def left_pinch(self, close_frames: int = 4):
        """Fingers apart (arms the pinch), then closed."""
        self.hold(2, pose=POINT, pinch_index=1.0)
        return self.hold(close_frames, pose=POINT, pinch_index=0.1)

    def left_open(self, n: int = 2):
        return self.hold(n, pose=POINT, pinch_index=1.0)


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive the touchpad's clock by hand: clock.t = ..."""
    class Clock:
        t = 1000.0

    clock = Clock()
    monkeypatch.setattr(tpm, "_now", lambda: clock.t)
    return clock
