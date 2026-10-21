"""Finger states, pose classification and the hand measurements built on them."""

import numpy as np
import pytest
from conftest import make_hand, rot_y

from gesture_control.features import (
    FIST,
    INDEX_TIP,
    MIDDLE_MCP,
    NONE,
    PALM,
    POINT,
    TWO,
    WRIST,
    anchor_point,
    classify_pose,
    compute_features,
    hand_spread,
    palm_length,
)

EXT, CURL = 1.15, 0.95  # the shipped pose.extend_ratio / curl_ratio
ALL = ("index", "middle", "ring", "pinky")


def _states(up):
    return ({f: f in up for f in ALL}, {f: f not in up for f in ALL})


@pytest.mark.parametrize("up, pose", [
    ((), FIST),
    (ALL, PALM),
    (("index",), POINT),
    (("index", "middle"), TWO),
    (("index", "pinky"), NONE),         # rock sign: not a gesture
    (("middle",), NONE),
    (("index", "middle", "ring"), NONE),
])
def test_classify_pose(up, pose):
    assert classify_pose(*_states(up)) == pose


def test_in_between_finger_is_neither_up_nor_curled():
    # Index half bent: not extended, not curled -> no fist and no point.
    extended = {"index": False, "middle": False, "ring": False, "pinky": False}
    curled = {"index": False, "middle": True, "ring": True, "pinky": True}
    assert classify_pose(extended, curled) == NONE


@pytest.mark.parametrize("up, pose", [
    (("index",), POINT),
    (("index", "middle"), TWO),
    (ALL, PALM),
    ((), FIST),
])
def test_synthetic_hands_get_their_pose(up, pose):
    f = compute_features(make_hand(up), EXT, CURL)
    assert f.pose == pose
    for name in ALL:
        assert f.extended[name] == (name in up)


def test_features_do_not_depend_on_hand_size_or_position():
    """Same hand at another distance (scale) and place (shift): same features."""
    a = compute_features(make_hand(("index", "middle")), EXT, CURL)
    b = compute_features(make_hand(("index", "middle")) * 0.07 + (3, -2, 1), EXT, CURL)
    assert b.pose == a.pose
    assert b.pinch_index == pytest.approx(a.pinch_index)
    assert b.reach_index == pytest.approx(a.reach_index)
    assert b.facing == pytest.approx(a.facing)
    for name in ALL:
        assert b.finger_ratio[name] == pytest.approx(a.finger_ratio[name])


def test_pinch_distance_is_small_only_when_touching():
    open_ = compute_features(make_hand(("index", "middle")), EXT, CURL)
    left = compute_features(make_hand(("index", "middle"), pinch_to="index"), EXT, CURL)
    right = compute_features(make_hand(("index", "middle"), pinch_to="middle"), EXT, CURL)
    assert open_.pinch_index > 0.42 and open_.pinch_middle > 0.42   # above pinch.release
    assert left.pinch_index == pytest.approx(0.0)
    assert left.pinch_middle > 0.28                                 # middle still open
    assert right.pinch_middle == pytest.approx(0.0)


def test_facing_drops_as_the_hand_turns_edge_on():
    hand = make_hand(("index",))
    square = compute_features(hand, EXT, CURL).facing
    turned = compute_features(rot_y(hand, 60), EXT, CURL).facing
    edge = compute_features(rot_y(hand, 90), EXT, CURL).facing
    assert square == pytest.approx(1.0)
    assert turned == pytest.approx(0.5, abs=0.01)    # cos 60
    assert edge == pytest.approx(0.0, abs=1e-9)
    # The back of the hand counts as facing too.
    assert compute_features(rot_y(hand, 180), EXT, CURL).facing == pytest.approx(1.0)


def test_reach_is_short_for_a_fist():
    point = compute_features(make_hand(("index",)), EXT, CURL)
    fist = compute_features(make_hand(()), EXT, CURL)
    assert point.reach_index >= 1.05 > fist.reach_index   # pinch.min_finger_reach


def test_degenerate_hand_does_not_crash():
    f = compute_features(np.zeros((21, 3)), EXT, CURL)
    assert f.facing == 0.0
    assert np.isfinite(f.pinch_index)


# -- image-space helpers -----------------------------------------------------------

def _image_hand(shift=(0.5, 0.6), scale=0.08):
    """make_hand in normalized image coords (x, y in 0..1)."""
    pts = make_hand(("index",)) * scale
    pts[:, 0] += shift[0]
    pts[:, 1] += shift[1]
    return pts


def test_anchor_knuckles_is_the_mean_of_the_four_knuckles():
    pts = _image_hand()
    x, y = anchor_point(pts, "knuckles")
    assert (x, y) == pytest.approx(tuple(pts[[5, 9, 13, 17], :2].mean(axis=0)))


def test_anchor_knuckles_stays_put_when_pinching():
    """The reason knuckles is the default: a pinch doesn't move the cursor."""
    open_ = make_hand(("index",))
    pinched = make_hand(("index",), pinch_to="index")
    pinched[8] += (0.1, 0.2, 0)  # the fingertip bends towards the thumb
    assert anchor_point(open_, "knuckles") == anchor_point(pinched, "knuckles")
    assert anchor_point(open_, "index_tip") != anchor_point(pinched, "index_tip")


@pytest.mark.parametrize("name, idx", [("index_tip", [INDEX_TIP]), ("index_mcp", [5]),
                                       ("palm_center", [WRIST, 5, MIDDLE_MCP, 17])])
def test_other_anchors(name, idx):
    pts = _image_hand()
    assert anchor_point(pts, name) == pytest.approx(tuple(pts[idx, :2].mean(axis=0)))


def test_palm_length_is_in_frame_widths():
    pts = np.zeros((21, 3))
    pts[MIDDLE_MCP] = (0.0, 0.3, 0)       # straight down, 0.3 frame heights
    # 640x480: 0.3 heights = 144 px = 0.225 widths
    assert palm_length(pts, 640, 480) == pytest.approx(0.225)


def test_hand_spread_is_in_palm_lengths_and_ignores_distance():
    a, b = _image_hand((0.3, 0.6)), _image_hand((0.7, 0.6))
    near = hand_spread(a, b, 640, 480)
    # Same hands twice as far from the camera: half the size, half the gap.
    a2, b2 = _image_hand((0.4, 0.6), 0.04), _image_hand((0.6, 0.6), 0.04)
    assert hand_spread(a2, b2, 640, 480) == pytest.approx(near)
    # 0.4 frame widths apart = 0.4 * 640/480 heights; palm = 1.05 * 0.08 heights
    assert near == pytest.approx(0.4 * 640 / 480 / (1.05 * 0.08), rel=0.01)
