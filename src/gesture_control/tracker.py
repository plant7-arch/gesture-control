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
    def __init__(self, cfg: dict[str, Any]):
        model = Path(cfg["model"])
        if not model.exists():
            raise FileNotFoundError(model)
        options = vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=int(cfg["num_hands"]),
            min_hand_detection_confidence=float(cfg["min_detection_confidence"]),
            min_hand_presence_confidence=float(cfg["min_presence_confidence"]),
            min_tracking_confidence=float(cfg["min_tracking_confidence"]),
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._last_ts = -1

    def process(self, frame_bgr: np.ndarray, timestamp_ms: int) -> Hand | None:
        """Detect the most confident hand in a BGR frame."""
        # VIDEO mode requires strictly increasing timestamps.
        ts = max(int(timestamp_ms), self._last_ts + 1)
        self._last_ts = ts

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, ts)
        if not result.hand_landmarks:
            return None

        best = 0
        if len(result.handedness) > 1:
            best = int(np.argmax([h[0].score for h in result.handedness]))

        img = np.array([[p.x, p.y, p.z] for p in result.hand_landmarks[best]], dtype=float)
        world = None
        if result.hand_world_landmarks:
            world = np.array(
                [[p.x, p.y, p.z] for p in result.hand_world_landmarks[best]], dtype=float
            )
        label, score = "?", 0.0
        if result.handedness:
            cat = result.handedness[best][0]
            label, score = cat.category_name, float(cat.score)
        return Hand(image=img, world=world, handedness=label, score=score)

    def close(self) -> None:
        self._landmarker.close()


def feature_points(hand: Hand, frame_w: int, frame_h: int) -> np.ndarray:
    """Landmarks in an isotropic 3D space for distance/angle features."""
    if hand.world is not None:
        return hand.world
    pts = hand.image.copy()
    aspect = frame_w / max(frame_h, 1)
    pts[:, 0] *= aspect
    pts[:, 2] *= aspect
    return pts
