"""The adaptive two-hand tracking logic, with fake landmarkers (no model needed)."""

from __future__ import annotations

import numpy as np
import pytest

from gesture_control.tracker import Hand, HandTracker


def _hand(tag: str) -> Hand:
    return Hand(image=np.zeros((21, 3)), world=None, handedness=tag, score=0.9)


class Script:
    """What each fake landmarker 'sees' this frame, and a log of which ran."""

    def __init__(self):
        self.hands_in_view = 1
        self.calls: list[tuple[int, str]] = []
        self.frame = 0

    def detect(self, landmarker: str):
        self.calls.append((self.frame, landmarker))
        n = 1 if landmarker == "one" else 2
        return [_hand(f"{landmarker}{i}") for i in range(min(n, self.hands_in_view))]


@pytest.fixture
def tracker():
    script = Script()
    trk = HandTracker.__new__(HandTracker)   # skip loading the model
    trk._adaptive, trk._every = True, 5
    trk._two_active, trk._misses, trk._frame, trk._last_ts = False, 0, 0, -1
    trk._landmarker, trk._one = "two", "one"
    trk._detect = lambda lm, image, ts: script.detect(lm)
    frame = np.zeros((48, 64, 3), np.uint8)

    def step(n=1):
        out = None
        for _ in range(n):
            script.frame += 1
            out = trk.process_all(frame, script.frame * 33)
        return out

    return trk, script, step


def test_one_hand_landmarker_sees_every_frame(tracker):
    """Its tracking must never skip a frame: on check frames it runs too."""
    trk, script, step = tracker
    step(20)
    ones = [f for f, lm in script.calls if lm == "one"]
    twos = [f for f, lm in script.calls if lm == "two"]
    assert ones == list(range(1, 21))
    assert twos == [5, 10, 15, 20]                 # second-hand checks only


def test_check_frame_keeps_the_one_hand_result_for_the_same_hand(tracker):
    """With one hand in view, a check frame returns the one-hand landmarker's hand,
    not the two-hand landmarker's (whose tracking is 5 frames old: it made the
    landmarks jump slightly every check frame)."""
    trk, script, step = tracker
    hands = step(5)                                # frame 5 is a check frame
    assert [h.handedness for h in hands] == ["one0"]
    assert not trk.two_hand_mode


def test_second_hand_switches_to_two_hand_tracking_and_back(tracker):
    trk, script, step = tracker
    step(6)
    script.hands_in_view = 2
    step(3)                                        # frames 7-9: not a check frame yet
    assert not trk.two_hand_mode
    hands = step(1)                                # frame 10: check frame finds both
    assert len(hands) == 2 and trk.two_hand_mode
    hands = step(4)
    assert len(hands) == 2 and script.calls[-1][1] == "two"
    script.hands_in_view = 1
    step(4)
    assert trk.two_hand_mode                       # a few misses are tolerated
    step(1)
    assert not trk.two_hand_mode                   # 5 misses: back to the fast mode
    step(1)
    assert (script.frame, "one") in script.calls  # the one-hand landmarker is back


def test_no_hand_for_one_landmarker_but_found_on_a_check_frame(tracker):
    trk, script, step = tracker
    script.hands_in_view = 0
    assert step(4) == []
    script.hands_in_view = 1
    original = script.detect
    script.detect = lambda lm: [] if lm == "one" else original(lm)  # one-hand misses it
    hands = step(1)                                # frame 5: check frame
    assert [h.handedness for h in hands] == ["two0"] and not trk.two_hand_mode
