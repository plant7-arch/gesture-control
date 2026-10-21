"""Main-loop helpers: which hand drives the cursor, and spotting hand switches."""

from types import SimpleNamespace

import numpy as np
import pytest

from gesture_control.app import MAX_JUMP_PALMS, _is_switch, _pick_primary, parse_args


def _hand(x, y):
    pts = np.zeros((21, 3))
    pts[:, 0], pts[:, 1] = x, y
    return SimpleNamespace(image=pts)


def test_single_hand_is_primary():
    h = _hand(0.5, 0.5)
    assert _pick_primary([h], (0.1, 0.1), "knuckles") is h
    assert _pick_primary([], None, "knuckles") is None


def test_second_hand_does_not_steal_the_cursor():
    """The tracker lists the most confident hand first; the primary hand is the one
    nearest to where the cursor hand was, whatever the order."""
    mine, newcomer = _hand(0.3, 0.5), _hand(0.7, 0.5)
    assert _pick_primary([newcomer, mine], (0.31, 0.5), "knuckles") is mine
    assert _pick_primary([mine, newcomer], (0.31, 0.5), "knuckles") is mine


def test_without_history_the_most_confident_hand_wins():
    a, b = _hand(0.3, 0.5), _hand(0.7, 0.5)
    assert _pick_primary([b, a], None, "knuckles") is b


PALM = 0.1


def test_normal_movement_is_not_a_switch():
    assert not _is_switch((0.52, 0.5), (0.5, 0.5), None, PALM)


def test_impossible_jump_is_a_switch():
    jump = (MAX_JUMP_PALMS + 0.2) * PALM
    assert _is_switch((0.5 + jump, 0.5), (0.5, 0.5), None, PALM)


def test_following_the_other_hand_is_a_switch():
    # Now nearer to where the *other* hand was than to our own last position.
    assert _is_switch((0.69, 0.5), (0.5, 0.5), (0.7, 0.5), PALM)


@pytest.mark.parametrize("anchor, prev", [(None, (0.5, 0.5)), ((0.5, 0.5), None)])
def test_no_history_is_not_a_switch(anchor, prev):
    assert not _is_switch(anchor, prev, (0.9, 0.9), PALM)


def test_unknown_palm_size_skips_the_jump_check():
    assert not _is_switch((0.9, 0.5), (0.1, 0.5), None, None)


def test_cli_flags():
    a = parse_args(["--dry-run", "--mode", "absolute", "--camera", "2", "-v"])
    assert a.dry_run and a.verbose and a.mode == "absolute" and a.camera == 2
    with pytest.raises(SystemExit):
        parse_args(["--mode", "sideways"])
