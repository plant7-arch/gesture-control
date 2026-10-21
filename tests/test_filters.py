"""One Euro filter: steady when still, quick to follow fast movement."""

import numpy as np
import pytest

from gesture_control.filters import OneEuroFilter

DT = 1 / 30


def _run(f, xs, t0=0.0):
    return [float(np.atleast_1d(f(x, t0 + i * DT))[0]) for i, x in enumerate(xs)]


def test_first_sample_passes_through():
    assert _run(OneEuroFilter(), [0.7]) == [0.7]


def test_constant_signal_stays_constant():
    out = _run(OneEuroFilter(1.0, 4.0), [0.3] * 30)
    assert out == pytest.approx([0.3] * 30)


def test_step_is_smoothed_then_reached():
    out = _run(OneEuroFilter(1.0, 0.0), [0.0] + [1.0] * 120)
    assert 0.0 < out[1] < 0.5          # smoothed, not a jump
    assert out == sorted(out)          # no overshoot
    assert out[-1] == pytest.approx(1.0, abs=1e-3)


def test_jitter_is_reduced():
    rng = np.random.default_rng(0)
    noisy = 0.5 + rng.normal(0, 0.01, 300)
    out = np.array(_run(OneEuroFilter(1.0, 4.0), noisy))
    assert out[30:].std() < noisy[30:].std() / 2


def test_beta_reduces_lag_on_fast_movement():
    ramp = [i * 0.02 for i in range(60)]
    slow = _run(OneEuroFilter(1.0, 0.0), ramp)
    fast = _run(OneEuroFilter(1.0, 10.0), ramp)
    assert abs(ramp[-1] - fast[-1]) < abs(ramp[-1] - slow[-1]) / 2


def test_repeated_or_backwards_time_returns_last_value():
    f = OneEuroFilter()
    f(0.0, 1.0)
    v = float(f(1.0, 1.0 + DT))
    assert float(f(5.0, 1.0 + DT)) == v       # same timestamp
    assert float(f(5.0, 1.0)) == v            # time went backwards


def test_reset_forgets_history():
    f = OneEuroFilter()
    _run(f, [0.0] * 10)
    f.reset()
    assert float(f(0.9, 50.0)) == 0.9


def test_vectors_share_one_cutoff():
    f = OneEuroFilter(1.0, 4.0)
    f([0.0, 0.0], 0.0)
    x, y = f([1.0, 2.0], DT)
    assert y == pytest.approx(2 * x)   # same smoothing on both axes keeps direction
