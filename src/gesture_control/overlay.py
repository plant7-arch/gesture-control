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
MODE_COLOUR = {Mode.IDLE: GREY, Mode.MOVE: GREEN, Mode.SCROLL: CYAN, Mode.ZOOM: MAGENTA,
               Mode.KEYBOARD: YELLOW}

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
    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, w), min(y1, h)
    roi = img[y0:y1, x0:x1]
    img[y0:y1, x0:x1] = (roi * k).astype(img.dtype)


def _arrow(img, r, direction: str, colour) -> None:
    """A filled triangle in the key rectangle r (the font has no arrow characters)."""
    cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    s = max(4, min(r[2] - r[0], r[3] - r[1]) // 4)
    pts = {"left": [(cx - s, cy), (cx + s, cy - s), (cx + s, cy + s)],
           "right": [(cx + s, cy), (cx - s, cy - s), (cx - s, cy + s)],
           "up": [(cx, cy - s), (cx - s, cy + s), (cx + s, cy + s)],
           "down": [(cx, cy + s), (cx - s, cy - s), (cx + s, cy - s)]}[direction]
    cv2.fillPoly(img, [np.array(pts, np.int32)], colour, cv2.LINE_AA)


class Overlay:
    # The question under "No camera frames" (app.py sets the phone's version).
    stall_hint = "is another app using the camera?"

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
        from .keyboard import Layout

        self.layout = Layout.from_config(cfg)
        self.toggle_name = cfg["keyboard"]["toggle"]
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
        if st.mode == Mode.KEYBOARD and not st.paused:
            return self._draw_keyboard(frame, st, fps, latency_ms)
        if anchor is not None and st.mode != Mode.ZOOM:
            a = (int(anchor[0] * w), int(anchor[1] * h))
            cv2.circle(frame, a, 8, colour, -1, cv2.LINE_AA)  # the point that drives the cursor
            if st.left_down:
                cv2.circle(frame, a, 14, RED, 3, cv2.LINE_AA)

        # Info panel
        _shade(frame, 0, 0, 250, 212)
        mode = "PAUSED" if st.paused else st.mode.value.upper()
        _text(frame, mode, (10, 28), colour, 0.8, 2)
        if st.mode == Mode.SCROLL and not st.paused:
            # Which way this swipe scrolls (the axis lock), once decided.
            axis = {"vertical": "up/down", "horizontal": "sideways",
                    "both": "any way"}.get(st.scroll_axis, "")
            if axis:
                _text(frame, axis, (125, 28), colour, 0.5, 1)
        fps_txt = f"{fps:4.1f} fps" if fps > 0 else "-- fps"
        _text(frame, f"{fps_txt}   {latency_ms:4.0f} ms", (10, 52))
        hand_txt = f"pose: {st.pose}" + (f" ({st.raw_pose})" if st.raw_pose != st.pose else "")
        _text(frame, hand_txt if st.hand else "no hand", (10, 74), WHITE if st.hand else GREY)
        if handedness and st.hand:
            _text(frame, handedness, (190, 74), GREY)
        self._meter(frame, "L", st.pinch_index, 96, st.pinch_blocked, st.left_armed or st.left_down)
        self._meter(frame, "R", st.pinch_middle, 116, st.pinch_blocked, st.right_armed)
        # Thumb to ring / pinky fingertip: the typing gestures (Enter / Backspace).
        self._meter(frame, "Rg", st.pinch_ring, 136, st.pinch_blocked, st.ring_armed)
        self._meter(frame, "Pk", st.pinch_pinky, 156, st.pinch_blocked, st.pinky_armed)
        if st.facing is not None:
            if st.pinch_blocked:
                _text(frame, f"facing {st.facing:.2f}  turn palm to camera", (10, 184), ORANGE, 0.45)
            else:
                _text(frame, f"facing {st.facing:.2f}", (10, 184), GREY, 0.45)
        if st.left_block:
            # Fingertips closed but no click: the reason, where the last action goes.
            _text(frame, f"L: {st.left_block}", (10, 204), ORANGE, 0.4)
        elif st.last_action:
            _text(frame, st.last_action, (10, 204), GREY, 0.45)

        if st.zoom_blocked:
            _shade(frame, 0, h - 44, w, h)
            why = ("touchpad turns off when a mouse is connected"
                   if st.zoom_blocked_state == "disabled-on-external-mouse"
                   else "touchpad is turned off in Settings")
            _text(frame, f"Zoom is off: {why} (see terminal)", (12, h - 18), MAGENTA, 0.48)
            return frame

        # Bottom band: fist-hold progress (pause/resume), keyboard-pose progress, or the
        # paused hint.
        if st.fist_progress > 0 or st.paused or st.toggle_progress > 0 or st.kb_warn:
            _shade(frame, 0, h - 44, w, h)
        if st.toggle_progress > 0:
            self._progress(frame, "keep holding to open the keyboard", st.toggle_progress)
        elif st.kb_warn:
            _text(frame, st.kb_warn, (12, h - 18), ORANGE, 0.42)
        elif st.fist_progress > 0:
            bw = int((w - 40) * st.fist_progress)
            _text(frame, "keep holding to " + ("resume" if st.paused else "pause"),
                  (20, h - 26), WHITE, 0.5)
            cv2.rectangle(frame, (20, h - 18), (w - 20, h - 8), GREY, 1)
            cv2.rectangle(frame, (20, h - 18), (20 + bw, h - 8), RED, -1)
        elif st.paused:
            _text(frame, "PAUSED - hold a fist or press p to resume", (20, h - 18), RED, 0.55)
        return frame

    def _progress(self, frame, text: str, progress: float) -> None:
        h, w = frame.shape[:2]
        _text(frame, text, (20, h - 26), WHITE, 0.5)
        cv2.rectangle(frame, (20, h - 18), (w - 20, h - 8), GREY, 1)
        cv2.rectangle(frame, (20, h - 18), (20 + int((w - 40) * progress), h - 8), YELLOW, -1)

    def _draw_keyboard(self, frame: np.ndarray, st: Status, fps: float,
                       latency_ms: float) -> np.ndarray:
        """Keyboard mode: the keys over the camera image, the fingertip, and a band at
        the bottom with what was typed."""
        h, w = frame.shape[:2]
        x0, x1, y0, y1 = self.layout.region
        _shade(frame, int(x0 * w) - 4, int(y0 * h) - 4, int(x1 * w) + 4, int(y1 * h) + 4, 0.55)
        shift = "shift" in st.kb_latched
        for i, k in enumerate(self.layout.keys):
            kx0, ky0, kx1, ky1 = k.rect
            r = (int(kx0 * w) + 2, int(ky0 * h) + 2, int(kx1 * w) - 2, int(ky1 * h) - 2)
            fill = None
            if i == st.kb_flash:
                fill = GREEN
            elif i == st.kb_hover:
                fill = YELLOW
            elif ((k.kind == "mod" and k.name in st.kb_latched)
                  or (k.kind == "caps" and st.kb_caps)
                  or (k.name == "alt+tab" and st.kb_switching)):
                fill = CYAN
            if fill is not None:
                cv2.rectangle(frame, r[:2], r[2:], fill, -1)
            edge = GREY if k.row < self.layout.shortcut_rows else WHITE
            cv2.rectangle(frame, r[:2], r[2:], edge, 1)
            ink = BLACK if fill is not None else WHITE
            if k.name in ("left", "up", "down", "right"):
                _arrow(frame, r, k.name, ink)
                continue
            label = k.label
            if k.kind == "char" and len(k.label) == 1:
                # What the key will type now: Shift and Caps (letters only) applied.
                upper = shift != (st.kb_caps and k.label.isalpha())
                label = k.shifted if upper else k.label.lower()
            scale = 0.5 if len(label) <= 2 else 0.4 if len(label) <= 5 else 0.34
            (tw, th), _ = cv2.getTextSize(label, FONT, scale, 1)
            cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
            _text(frame, label, (cx - tw // 2, cy + th // 2), ink, scale)
        if st.tip is not None:
            p = (int(st.tip[0] * w), int(st.tip[1] * h))
            # A ring, so the label of the key under it stays readable.
            cv2.circle(frame, p, 9, BLACK, 3, cv2.LINE_AA)
            cv2.circle(frame, p, 9, MAGENTA, 2, cv2.LINE_AA)

        # Bottom band: mode and timing, then what was typed (or a hint, a progress bar).
        _shade(frame, 0, h - 52, w, h, 0.3)
        fps_txt = f"{fps:4.1f} fps" if fps > 0 else "-- fps"
        _text(frame, "KEYBOARD", (10, h - 32), YELLOW, 0.55, 2)
        _text(frame, f"{fps_txt} {latency_ms:4.0f} ms", (125, h - 32), GREY, 0.42)
        if st.left_block:
            _text(frame, f"L: {st.left_block}", (260, h - 32), ORANGE, 0.4)
        else:
            _text(frame, f"hold {self.toggle_name} to close", (260, h - 32), GREY, 0.42)
        if st.toggle_progress > 0:
            cv2.rectangle(frame, (10, h - 18), (w - 10, h - 8), GREY, 1)
            cv2.rectangle(frame, (10, h - 18), (10 + int((w - 20) * st.toggle_progress), h - 8),
                          YELLOW, -1)
        elif st.kb_warn:
            _text(frame, st.kb_warn, (10, h - 10), ORANGE, 0.42)
        else:
            mods = " ".join(m.capitalize() for m in st.kb_latched)
            text = st.kb_text[-48:]
            line = (f"[{mods}] " if mods else "") + "> " + text + "_"
            _text(frame, line, (10, h - 10), WHITE, 0.5)
        return frame

    def _meter(self, frame, label: str, value: float | None, y: int,
               blocked: bool = False, armed: bool = False) -> None:
        """Pinch distance bar: fills as fingers close; ticks mark start/release.
        Grey while pinches are blocked because the hand is turned away. The label
        lights up green when the pinch is armed (the next close will click)."""
        x, width = 30, 150
        size = 0.5 if len(label) == 1 else 0.36
        _text(frame, label, (10, y + 9), GREEN if armed else GREY, size, 2 if armed else 1)
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
        _shade(img, 0, h // 2 - 40, w, h // 2 + 64, 0.3)
        _text(img, f"No camera frames for {seconds:.0f} s", (20, h // 2 - 10), ORANGE, 0.7, 2)
        hint = self.stall_hint
        _text(img, hint[0].upper() + hint[1:], (20, h // 2 + 18), WHITE, 0.45)
        _text(img, "Buttons and keys are released.", (20, h // 2 + 38), WHITE, 0.45)
        _text(img, "q / Esc to quit", (20, h // 2 + 58), GREY, 0.45)
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
