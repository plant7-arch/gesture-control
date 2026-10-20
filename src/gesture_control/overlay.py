"""Debug preview: landmarks, active region, mode, pinch meters and timing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .controller import Mode, Status
from .features import HAND_CONNECTIONS, INDEX_TIP, MIDDLE_TIP, THUMB_TIP
from .tracker import Hand

# BGR colours
WHITE, GREY, BLACK = (255, 255, 255), (150, 150, 150), (0, 0, 0)
GREEN, CYAN, ORANGE, RED, YELLOW = (80, 220, 80), (230, 200, 40), (40, 160, 255), (60, 60, 230), (40, 220, 240)
MODE_COLOUR = {Mode.IDLE: GREY, Mode.MOVE: GREEN, Mode.SCROLL: CYAN}

FONT = cv2.FONT_HERSHEY_SIMPLEX


def fix_qt_fonts() -> None:
    """opencv-python's bundled Qt looks for fonts in a folder that doesn't exist
    and warns on every window. Point it at the system fonts instead. Must run
    after `import cv2` (which sets QT_QPA_FONTDIR) and before the first window."""
    current = os.environ.get("QT_QPA_FONTDIR")
    if current and Path(current).is_dir():
        return
    for candidate in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype",
                      "/usr/share/fonts"):
        if Path(candidate).is_dir():
            os.environ["QT_QPA_FONTDIR"] = candidate
            return


def _text(img, s, org, colour=WHITE, scale=0.5, thick=1):
    cv2.putText(img, s, org, FONT, scale, colour, thick, cv2.LINE_AA)


def _shade(img, x0, y0, x1, y1, k=0.4):
    """Darken a rectangle so text on top of it is readable."""
    roi = img[y0:y1, x0:x1]
    img[y0:y1, x0:x1] = (roi * k).astype(img.dtype)


class Overlay:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.name = cfg["preview"]["window_name"]
        self.show_landmarks = bool(cfg["preview"]["show_landmarks"])
        r = cfg["cursor"]["region"]
        self.region = (r["x_min"], r["x_max"], r["y_min"], r["y_max"])
        self.pinch_start = float(cfg["pinch"]["start"])
        self.pinch_release = float(cfg["pinch"]["release"])
        fix_qt_fonts()
        cv2.namedWindow(self.name, cv2.WINDOW_AUTOSIZE)

    def draw(self, frame: np.ndarray, hand: Hand | None, st: Status,
             fps: float, latency_ms: float, handedness: str = "",
             anchor: tuple[float, float] | None = None) -> np.ndarray:
        h, w = frame.shape[:2]
        colour = RED if st.paused else MODE_COLOUR[st.mode]

        # Active region (the part of the camera view that maps to the screen).
        x0, x1, y0, y1 = self.region
        cv2.rectangle(frame, (int(x0 * w), int(y0 * h)), (int(x1 * w), int(y1 * h)), colour, 1)

        if hand is not None and self.show_landmarks:
            pts = [(int(p[0] * w), int(p[1] * h)) for p in hand.image]
            for a, b in HAND_CONNECTIONS:
                cv2.line(frame, pts[a], pts[b], colour, 2, cv2.LINE_AA)
            for p in pts:
                cv2.circle(frame, p, 3, WHITE, -1, cv2.LINE_AA)
            for i, c in ((THUMB_TIP, YELLOW), (INDEX_TIP, YELLOW), (MIDDLE_TIP, ORANGE)):
                cv2.circle(frame, pts[i], 6, c, 2, cv2.LINE_AA)
        if anchor is not None:
            a = (int(anchor[0] * w), int(anchor[1] * h))
            cv2.circle(frame, a, 8, colour, -1, cv2.LINE_AA)  # the point that drives the cursor
            if st.left_down:
                cv2.circle(frame, a, 14, RED, 3, cv2.LINE_AA)

        # Info panel
        _shade(frame, 0, 0, 250, 150)
        mode = "PAUSED" if st.paused else st.mode.value.upper()
        _text(frame, mode, (10, 28), colour, 0.8, 2)
        fps_txt = f"{fps:4.1f} fps" if fps > 0 else "-- fps"
        _text(frame, f"{fps_txt}   {latency_ms:4.0f} ms", (10, 52))
        hand_txt = f"pose: {st.pose}" + (f" ({st.raw_pose})" if st.raw_pose != st.pose else "")
        _text(frame, hand_txt if st.hand else "no hand", (10, 74), WHITE if st.hand else GREY)
        if handedness and st.hand:
            _text(frame, handedness, (190, 74), GREY)
        self._meter(frame, "L", st.pinch_index, 96)
        self._meter(frame, "R", st.pinch_middle, 116)
        if st.last_action:
            _text(frame, st.last_action, (10, 140), GREY, 0.45)

        # Bottom band: fist-hold progress (pause/resume), or the paused hint.
        if st.fist_progress > 0 or st.paused:
            _shade(frame, 0, h - 44, w, h)
        if st.fist_progress > 0:
            bw = int((w - 40) * st.fist_progress)
            _text(frame, "keep holding to " + ("resume" if st.paused else "pause"),
                  (20, h - 26), WHITE, 0.5)
            cv2.rectangle(frame, (20, h - 18), (w - 20, h - 8), GREY, 1)
            cv2.rectangle(frame, (20, h - 18), (20 + bw, h - 8), RED, -1)
        elif st.paused:
            _text(frame, "PAUSED - hold a fist or press p to resume", (20, h - 18), RED, 0.55)
        return frame

    def _meter(self, frame, label: str, value: float | None, y: int) -> None:
        """Pinch distance bar: fills as fingers close; ticks mark start/release."""
        x, width = 30, 150
        _text(frame, label, (10, y + 9), GREY, 0.45)
        cv2.rectangle(frame, (x, y), (x + width, y + 10), GREY, 1)
        top = 1.0
        if value is not None:
            fill = int(width * max(0.0, 1.0 - min(value, top) / top))
            c = RED if value < self.pinch_start else ORANGE if value < self.pinch_release else GREEN
            cv2.rectangle(frame, (x, y), (x + fill, y + 10), c, -1)
            _text(frame, f"{value:.2f}", (x + width + 8, y + 10), GREY, 0.45)
        for thr in (self.pinch_start, self.pinch_release):
            tx = x + int(width * (1.0 - thr / top))
            cv2.line(frame, (tx, y - 2), (tx, y + 12), WHITE, 1)

    def show(self, frame: np.ndarray) -> int:
        cv2.imshow(self.name, frame)
        return cv2.waitKey(1) & 0xFF

    def poll(self) -> int:
        return cv2.waitKey(1) & 0xFF

    def closed(self) -> bool:
        try:
            return cv2.getWindowProperty(self.name, cv2.WND_PROP_VISIBLE) < 1
        except cv2.error:
            return True

    def close(self) -> None:
        cv2.destroyAllWindows()
