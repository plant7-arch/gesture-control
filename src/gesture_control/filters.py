"""One Euro filter: heavy smoothing when still, light smoothing when moving fast.

Reference: Casiez, Roussel & Vogel, "1€ Filter" (CHI 2012).
"""

from __future__ import annotations

import math

import numpy as np


def _alpha(dt: float, cutoff: float) -> float:
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class OneEuroFilter:
    """Filters a scalar or vector signal. All components share one cutoff,
    driven by the speed (vector norm), so x and y are smoothed consistently."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self.reset()

    def reset(self) -> None:
        self._t: float | None = None
        self._x: np.ndarray | None = None
        self._dx: np.ndarray | None = None

    def __call__(self, x, t: float) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if self._t is None or self._x is None:
            self._t, self._x, self._dx = t, x, np.zeros_like(x)
            return x
        dt = t - self._t
        if dt <= 0:
            return self._x
        dx = (x - self._x) / dt
        a_d = _alpha(dt, self.d_cutoff)
        dx_hat = a_d * dx + (1.0 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * float(np.linalg.norm(dx_hat))
        a = _alpha(dt, cutoff)
        x_hat = a * x + (1.0 - a) * self._x
        self._t, self._x, self._dx = t, x_hat, dx_hat
        return x_hat
