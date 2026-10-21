"""Debug preview: landmarks, active region, mode, pinch meters and timing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from .controller import Mode, Status
from .features import HAND_CONNECTIONS, INDEX_TIP, MIDDLE_TIP, THUMB_TIP

if TYPE_CHECKING:  # importing tracker pulls in mediapipe, which loads in the background
    from .tracker import Hand

# BGR colours
WHITE, GREY, BLACK = (255, 255, 255), (150, 150, 150), (0, 0, 0)
GREEN, CYAN, ORANGE, RED, YELLOW = (80, 220, 80), (230, 200, 40), (40, 160, 255), (60, 60, 230), (40, 220, 240)
MAGENTA = (220, 90, 220)
MODE_COLOUR = {Mode.IDLE: GREY, Mode.MOVE: GREEN, Mode.SCROLL: CYAN, Mode.ZOOM: MAGENTA}

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
        # The box only means something in absolute mode (it is mapped to the screen).
        self.show_region = cfg["cursor"]["mode"] == "absolute"
        self.pinch_start = float(cfg["pinch"]["start"])
        self.pinch_release = float(cfg["pinch"]["release"])
        fix_qt_fonts()
        # GUI_NORMAL: no toolbar (pan/zoom/save buttons) or status bar from OpenCV's
        # Qt window, just the image.
        cv2.namedWindow(self.name, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)

    def draw(self, frame: np.ndarray, hand: Hand | None, st: Status,
             fps: float, latency_ms: float, handedness: str = "",
             anchor: tuple[float, float] | None = None,
             other: Hand | None = None) -> np.ndarray:
        h, w = frame.shape[:2]
        colour = RED if st.paused else MODE_COLOUR[st.mode]

        # Active region (the part of the camera view that maps to the screen).
        if self.show_region:
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
        if other is not None and self.show_landmarks:
            # Second hand: drawn thinner; magenta while it's part of a zoom.
            oc = MAGENTA if st.mode == Mode.ZOOM else GREY
            opts = [(int(p[0] * w), int(p[1] * h)) for p in other.image]
            for a_, b_ in HAND_CONNECTIONS:
                cv2.line(frame, opts[a_], opts[b_], oc, 1, cv2.LINE_AA)
            cv2.circle(frame, opts[INDEX_TIP], 6, YELLOW, 2, cv2.LINE_AA)
            if hand is not None and (st.mode == Mode.ZOOM or st.zoom_hands):
                p1 = (int(hand.image[INDEX_TIP][0] * w), int(hand.image[INDEX_TIP][1] * h))
                cv2.line(frame, p1, opts[INDEX_TIP], MAGENTA, 2, cv2.LINE_AA)
                if st.zoom_spread is not None:
                    mid = ((p1[0] + opts[INDEX_TIP][0]) // 2, (p1[1] + opts[INDEX_TIP][1]) // 2 - 10)
                    _text(frame, f"{st.zoom_spread:.2f}", mid, MAGENTA, 0.55, 2)
        if anchor is not None and st.mode != Mode.ZOOM:
            a = (int(anchor[0] * w), int(anchor[1] * h))
            cv2.circle(frame, a, 8, colour, -1, cv2.LINE_AA)  # the point that drives the cursor
            if st.left_down:
                cv2.circle(frame, a, 14, RED, 3, cv2.LINE_AA)

        # Info panel
        _shade(frame, 0, 0, 250, 172)
        mode = "PAUSED" if st.paused else st.mode.value.upper()
        _text(frame, mode, (10, 28), colour, 0.8, 2)
        fps_txt = f"{fps:4.1f} fps" if fps > 0 else "-- fps"
        _text(frame, f"{fps_txt}   {latency_ms:4.0f} ms", (10, 52))
        hand_txt = f"pose: {st.pose}" + (f" ({st.raw_pose})" if st.raw_pose != st.pose else "")
        _text(frame, hand_txt if st.hand else "no hand", (10, 74), WHITE if st.hand else GREY)
        if handedness and st.hand:
            _text(frame, handedness, (190, 74), GREY)
        self._meter(frame, "L", st.pinch_index, 96, st.pinch_blocked, st.left_armed or st.left_down)
        self._meter(frame, "R", st.pinch_middle, 116, st.pinch_blocked, st.right_armed)
        if st.facing is not None:
            if st.pinch_blocked:
                _text(frame, f"facing {st.facing:.2f}  turn palm to camera", (10, 144), ORANGE, 0.45)
            else:
                _text(frame, f"facing {st.facing:.2f}", (10, 144), GREY, 0.45)
        if st.last_action:
            _text(frame, st.last_action, (10, 164), GREY, 0.45)

        if st.zoom_blocked:
            _shade(frame, 0, h - 44, w, h)
            why = ("touchpad turns off when a mouse is connected"
                   if st.zoom_blocked_state == "disabled-on-external-mouse"
                   else "touchpad is turned off in Settings")
            _text(frame, f"Zoom is off: {why} (see terminal)", (12, h - 18), MAGENTA, 0.48)
            return frame

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

    def _meter(self, frame, label: str, value: float | None, y: int,
               blocked: bool = False, armed: bool = False) -> None:
        """Pinch distance bar: fills as fingers close; ticks mark start/release.
        Grey while pinches are blocked because the hand is turned away. The label
        lights up green when the pinch is armed (the next close will click)."""
        x, width = 30, 150
        _text(frame, label, (10, y + 9), GREEN if armed else GREY, 0.5, 2 if armed else 1)
        cv2.rectangle(frame, (x, y), (x + width, y + 10), GREY, 1)
        top = 1.0
        if value is not None:
            fill = int(width * max(0.0, 1.0 - min(value, top) / top))
            c = RED if value < self.pinch_start else ORANGE if value < self.pinch_release else GREEN
            if blocked:
                c = GREY
            cv2.rectangle(frame, (x, y), (x + fill, y + 10), c, -1)
            _text(frame, f"{value:.2f}", (x + width + 8, y + 10), GREY, 0.45)
        for thr in (self.pinch_start, self.pinch_release):
            tx = x + int(width * (1.0 - thr / top))
            cv2.line(frame, (tx, y - 2), (tx, y + 12), WHITE, 1)

    def show(self, frame: np.ndarray) -> int:
        self._last = frame  # for show_stalled(); each frame is a new array, no copy needed
        cv2.imshow(self.name, frame)
        return cv2.waitKey(1) & 0xFF

    def stalled_image(self, seconds: float) -> np.ndarray:
        """The last frame, dimmed, with a band saying the camera has stopped sending.
        Without it the window just freezes and looks like the app hung."""
        last = getattr(self, "_last", None)
        if last is not None:
            img = (last * 0.35).astype(np.uint8)
        else:
            w, h = int(self.cfg["camera"]["width"]), int(self.cfg["camera"]["height"])
            img = np.full((h, w, 3), 32, np.uint8)
        h, w = img.shape[:2]
        _shade(img, 0, h // 2 - 40, w, h // 2 + 44, 0.3)
        _text(img, f"No camera frames for {seconds:.0f} s", (20, h // 2 - 10), ORANGE, 0.7, 2)
        _text(img, "Is another app using the camera? Buttons and keys are released.",
              (20, h // 2 + 18), WHITE, 0.45)
        _text(img, "q / Esc to quit", (20, h // 2 + 38), GREY, 0.45)
        return img

    def show_stalled(self, seconds: float) -> int:
        """Show stalled_image() and handle keys; the frame shown before stays _last."""
        cv2.imshow(self.name, self.stalled_image(seconds))
        return cv2.waitKey(1) & 0xFF

    def poll(self, wait_ms: int = 1) -> int:
        return cv2.waitKey(max(1, int(wait_ms))) & 0xFF

    def placeholder(self, text: str) -> None:
        """A dark frame with a message, shown while the camera and tracker start."""
        w, h = int(self.cfg["camera"]["width"]), int(self.cfg["camera"]["height"])
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:] = (32, 32, 32)
        _text(img, text, (20, h // 2), WHITE, 0.6, 1)
        _text(img, "q / Esc to quit", (20, h // 2 + 30), WHITE, 0.45, 1)
        cv2.imshow(self.name, img)
        cv2.waitKey(1)

    def closed(self) -> bool:
        try:
            return cv2.getWindowProperty(self.name, cv2.WND_PROP_VISIBLE) < 1
        except cv2.error:
            return True

    def close(self) -> None:
        cv2.destroyAllWindows()
