"""MediaPipe Hand Landmarker wrapper (Tasks API, VIDEO running mode)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision


@dataclass
class Hand:
    image: np.ndarray          # (21, 3) normalized image coords: x, y in [0, 1]
    world: np.ndarray | None   # (21, 3) metres, origin near the hand's centre
    handedness: str            # "Left" / "Right" (correct for a mirrored feed)
    score: float


class HandTracker:
    """Hand landmarks with adaptive two-hand tracking.

    Looking for a second hand is expensive: with num_hands=2 and only one hand in view,
    MediaPipe runs its palm detector on every frame (about twice the cost per frame).
    So with num_hands=2 this keeps two landmarkers: a fast one-hand landmarker for normal
    use, a two-hand landmarker that also runs every `second_hand_check_every` frames to
    look for a second hand, and takes over while two hands are in view.
    """

    def __init__(self, cfg: dict[str, Any]):
        model = Path(cfg["model"])
        if not model.exists():
            raise FileNotFoundError(model)

        def make(n: int):
            options = vision.HandLandmarkerOptions(
                base_options=mp_tasks.BaseOptions(model_asset_path=str(model)),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=n,
                min_hand_detection_confidence=float(cfg["min_detection_confidence"]),
                min_hand_presence_confidence=float(cfg["min_presence_confidence"]),
                min_tracking_confidence=float(cfg["min_tracking_confidence"]),
            )
            return vision.HandLandmarker.create_from_options(options)

        n = int(cfg["num_hands"])
        every = int(cfg.get("second_hand_check_every", 0))
        self._adaptive = n >= 2 and every > 1
        self._landmarker = make(n)                     # n-hand landmarker
        self._one = make(1) if self._adaptive else None
        self._every = every
        self._two_active = False
        self._misses = 0
        self._frame = 0
        self._last_ts = -1

    def warm_up(self, width: int = 640, height: int = 480) -> None:
        """Run each model once on a blank image: the first inference is much slower
        (memory setup), so do it while waiting for the camera, not on the first frame."""
        blank = np.zeros((height, width, 3), dtype=np.uint8)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=blank)
        for lm in (self._landmarker, self._one):
            if lm is not None:
                lm.detect_for_video(image, 0)
        self._last_ts = 0

    def _detect(self, landmarker, image, ts) -> list[Hand]:
        result = landmarker.detect_for_video(image, ts)
        hands = []
        for i, lms in enumerate(result.hand_landmarks or []):
            img = np.array([[p.x, p.y, p.z] for p in lms], dtype=float)
            world = None
            if result.hand_world_landmarks and i < len(result.hand_world_landmarks):
                world = np.array([[p.x, p.y, p.z] for p in result.hand_world_landmarks[i]],
                                 dtype=float)
            label, score = "?", 0.0
            if result.handedness and i < len(result.handedness):
                cat = result.handedness[i][0]
                label, score = cat.category_name, float(cat.score)
            hands.append(Hand(image=img, world=world, handedness=label, score=score))
        # The Tasks API reports no per-hand detection or presence score; the
        # handedness score is the only per-hand confidence it gives, so hands are
        # ordered by it. It only decides which hand drives the cursor when there's
        # no previous position to follow (app._pick_primary).
        hands.sort(key=lambda h: h.score, reverse=True)
        return hands

    def process_all(self, frame_bgr: np.ndarray, timestamp_ms: int) -> list[Hand]:
        """All detected hands in a BGR frame, highest handedness score first."""
        # VIDEO mode requires strictly increasing timestamps.
        ts = max(int(timestamp_ms), self._last_ts + 1)
        self._last_ts = ts
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        if not self._adaptive:
            return self._detect(self._landmarker, image, ts)

        self._frame += 1
        if self._two_active:
            hands = self._detect(self._landmarker, image, ts)
            if len(hands) >= 2:
                self._misses = 0
            else:
                self._misses += 1
                if self._misses >= 5:  # second hand gone for a while: back to fast mode
                    self._two_active = False
            return hands
        # One-hand mode: the one-hand landmarker sees every frame, so its tracking
        # never skips a frame. On check frames the two-hand landmarker also runs, but
        # its result is only used if it found more hands. (Using it for the hand already
        # tracked made that hand's landmarks jump slightly every check frame: its
        # tracking state was `every` frames old. Measured on a moving hand: 0.63 px
        # average error on check frames vs 0.30 px; now 0.28 px on every frame.)
        hands = self._detect(self._one, image, ts)
        if self._frame % self._every == 0:
            more = self._detect(self._landmarker, image, ts)
            if len(more) > len(hands):
                if len(more) >= 2:
                    self._two_active, self._misses = True, 0
                return more
        return hands

    @property
    def two_hand_mode(self) -> bool:
        return self._two_active

    def process(self, frame_bgr: np.ndarray, timestamp_ms: int) -> Hand | None:
        """The most confident hand in a BGR frame."""
        hands = self.process_all(frame_bgr, timestamp_ms)
        return hands[0] if hands else None

    def close(self) -> None:
        self._landmarker.close()
        if self._one is not None:
            self._one.close()


def feature_points(hand: Hand, frame_w: int, frame_h: int) -> np.ndarray:
    """Landmarks in an isotropic 3D space for distance/angle features."""
    if hand.world is not None:
        return hand.world
    pts = hand.image.copy()
    aspect = frame_w / max(frame_h, 1)
    pts[:, 0] *= aspect
    pts[:, 2] *= aspect
    return pts
