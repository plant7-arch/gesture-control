"""Turn 21 hand landmarks into scale/rotation-tolerant features and a static pose."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# MediaPipe hand landmark indices
WRIST = 0
THUMB_TIP = 4
INDEX_MCP, INDEX_PIP, INDEX_TIP = 5, 6, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_TIP = 9, 10, 12
RING_MCP, RING_PIP, RING_TIP = 13, 14, 16
PINKY_MCP, PINKY_PIP, PINKY_TIP = 17, 18, 20

FINGERS: dict[str, tuple[int, int]] = {
    "index": (INDEX_PIP, INDEX_TIP),
    "middle": (MIDDLE_PIP, MIDDLE_TIP),
    "ring": (RING_PIP, RING_TIP),
    "pinky": (PINKY_PIP, PINKY_TIP),
}

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12),
    (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20),
    (0, 17),
]

# Poses
POINT = "point"   # index up, others down       -> move cursor
TWO = "two"       # index + middle up           -> scroll
PALM = "palm"     # all four fingers up         -> neutral
FIST = "fist"     # all four fingers curled     -> hold to pause/resume
NONE = "none"     # anything else / in between  -> keep current mode


@dataclass
class HandFeatures:
    finger_ratio: dict[str, float]  # dist(wrist, tip) / dist(wrist, pip)
    extended: dict[str, bool]
    curled: dict[str, bool]
    pinch_index: float   # dist(thumb tip, index tip) / palm size
    pinch_middle: float  # dist(thumb tip, middle tip) / palm size
    reach_index: float   # dist(wrist, index tip) / palm size
    reach_middle: float  # dist(wrist, middle tip) / palm size
    palm_size: float
    facing: float        # 1 = palm (or back of hand) square to the camera, 0 = edge-on
    pose: str


def _d(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def classify_pose(extended: dict[str, bool], curled: dict[str, bool]) -> str:
    ext = [extended[f] for f in ("index", "middle", "ring", "pinky")]
    if all(curled.values()):
        return FIST
    if all(ext):
        return PALM
    if ext[0] and not ext[1] and not ext[2] and not ext[3]:
        return POINT
    if ext[0] and ext[1] and not ext[2] and not ext[3]:
        return TWO
    return NONE


def compute_features(pts: np.ndarray, extend_ratio: float, curl_ratio: float) -> HandFeatures:
    """pts: (21, 3) landmarks in an isotropic space (world landmarks in metres,
    or image landmarks with x scaled by the aspect ratio)."""
    wrist = pts[WRIST]
    palm = max(_d(wrist, pts[MIDDLE_MCP]), 1e-6)

    ratio, extended, curled = {}, {}, {}
    for name, (pip, tip) in FINGERS.items():
        r = _d(wrist, pts[tip]) / max(_d(wrist, pts[pip]), 1e-6)
        ratio[name] = r
        extended[name] = r > extend_ratio
        curled[name] = r < curl_ratio

    # Palm normal from wrist, index base and pinky base. Its depth component says how
    # squarely the hand faces the camera; edge-on, fingertips can line up along the
    # camera's line of sight and look touching when they're not.
    normal = np.cross(pts[INDEX_MCP] - wrist, pts[PINKY_MCP] - wrist)
    n_len = float(np.linalg.norm(normal))
    facing = abs(float(normal[2])) / n_len if n_len > 1e-9 else 0.0

    return HandFeatures(
        finger_ratio=ratio,
        extended=extended,
        curled=curled,
        pinch_index=_d(pts[THUMB_TIP], pts[INDEX_TIP]) / palm,
        pinch_middle=_d(pts[THUMB_TIP], pts[MIDDLE_TIP]) / palm,
        reach_index=_d(wrist, pts[INDEX_TIP]) / palm,
        reach_middle=_d(wrist, pts[MIDDLE_TIP]) / palm,
        palm_size=palm,
        facing=facing,
        pose=classify_pose(extended, curled),
    )


def anchor_point(image_pts: np.ndarray, anchor: str) -> tuple[float, float]:
    """Point (normalized image coords) that drives the cursor.

    knuckles (default) and index_mcp barely move when you pinch, so clicks don't
    drag the cursor.
    """
    if anchor == "knuckles":
        # Average of four landmarks: about half the tracking jitter of one point,
        # and knuckles stay put when you pinch.
        p = image_pts[[INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP]].mean(axis=0)
    elif anchor == "index_tip":
        p = image_pts[INDEX_TIP]
    elif anchor == "palm_center":
        p = image_pts[[WRIST, INDEX_MCP, MIDDLE_MCP, PINKY_MCP]].mean(axis=0)
    else:
        p = image_pts[INDEX_MCP]
    return float(p[0]), float(p[1])


def palm_length(image_pts: np.ndarray, frame_w: int, frame_h: int) -> float:
    """Wrist to middle knuckle, in camera-frame widths."""
    dx = image_pts[MIDDLE_MCP][0] - image_pts[WRIST][0]
    dy = (image_pts[MIDDLE_MCP][1] - image_pts[WRIST][1]) * frame_h / max(frame_w, 1)
    return float((dx * dx + dy * dy) ** 0.5)
