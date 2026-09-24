"""Shared calibration helper.

Every strategy answers the same question during calibration: "on training data
only, how many basis points of forward return does one unit of my signal buy?"
The answer is a slope with a confidence interval, and the rule is uniform --
if the interval includes zero, the slope is set to zero, the strategy claims no
edge, and the engine will not place a trade for it.

That single rule is what stops an unprofitable idea from quietly trading anyway.
"""
from __future__ import annotations

import numpy as np


def fit_slope(x: np.ndarray, y_bps: np.ndarray, min_n: int = 200) -> dict:
    """Least-squares slope through the origin, with a heteroskedasticity-robust
    standard error (White 1980) because crypto return variance is anything but
    constant across the signal range.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y_bps, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    n = x.size
    if n < min_n:
        return {"beta": 0.0, "ci": (0.0, 0.0), "n": int(n), "se": None,
                "reason": f"only {n} training observations, need {min_n}"}
    denom = float(np.sum(x * x))
    if denom <= 0:
        return {"beta": 0.0, "ci": (0.0, 0.0), "n": int(n), "se": None,
                "reason": "signal has no variation on training data"}
    beta = float(np.sum(x * y) / denom)
    resid = y - beta * x
    # White robust variance for a through-origin slope
    se = float(np.sqrt(np.sum((x ** 2) * (resid ** 2)) / (denom ** 2)))
    lo, hi = beta - 1.96 * se, beta + 1.96 * se
    used = beta if lo > 0 else 0.0
    return {
        "beta": used, "beta_raw": beta, "ci": (lo, hi), "se": se, "n": int(n),
        "significant": bool(lo > 0),
        "reason": ("slope significantly positive" if lo > 0
                   else "confidence interval includes zero -- no edge claimed"),
    }
