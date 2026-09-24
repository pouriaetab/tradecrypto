"""Does 'get in early in the morning' or 'memes run on weekends' actually hold?

This is the module that answers a specific class of question honestly: the
operator has noticed calendar patterns, and calendar patterns are the single
easiest thing in finance to see when they are not there. With 24 hours x 7 days
there are 168 buckets; at a 5% significance level you expect about 8 of them to
look significant by pure chance.

So every bucket is tested, and then the whole family of p-values is corrected
with Benjamini-Hochberg, which controls the expected proportion of false
discoveries among the ones you decide to believe. A bucket that survives BH is
worth a second look. A bucket that does not is a coincidence you noticed.

Nothing here is a strategy on its own. It is a filter that tells a strategy
which hours are worth being awake for -- and, just as often, tells you that the
pattern you were sure about is noise.

References
----------
Benjamini, Y. & Hochberg, Y. (1995). Controlling the False Discovery Rate.
    JRSS-B 57(1).
Sullivan, Timmermann & White (2001). Dangers of data mining: the case of
    calendar effects in stock returns. Journal of Econometrics 105(1).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from scipy import stats as sps

from app.strategy.base import Panel


def benjamini_hochberg(pvals: np.ndarray, alpha: float = 0.10) -> np.ndarray:
    """Return a boolean mask of hypotheses that survive FDR control at alpha."""
    p = np.asarray(pvals, dtype=float)
    n = p.size
    if n == 0:
        return np.zeros(0, dtype=bool)
    order = np.argsort(p)
    thresh = alpha * (np.arange(1, n + 1) / n)
    passed = p[order] <= thresh
    keep = np.zeros(n, dtype=bool)
    if passed.any():
        cutoff = np.max(np.where(passed)[0])
        keep[order[: cutoff + 1]] = True
    return keep


def _buckets(ts: np.ndarray, tz_offset_hours: float) -> tuple[np.ndarray, np.ndarray]:
    shifted = ts + tz_offset_hours * 3600
    hours = np.array([datetime.fromtimestamp(t, tz=timezone.utc).hour for t in shifted])
    dows = np.array([datetime.fromtimestamp(t, tz=timezone.utc).weekday() for t in shifted])
    return hours, dows


def calendar_effects(
    panel: Panel,
    horizon_bars: int = 30,
    tz_offset_hours: float = -6.0,     # America/Chicago, the operator's clock
    alpha: float = 0.10,
    min_obs: int = 40,
) -> dict:
    """Forward return by hour-of-day and by day-of-week, FDR-corrected.

    The forward return is the CROSS-SECTIONAL MEAN over coins, so this measures
    "does the whole market tend to move at this hour", which is the operator's
    actual hypothesis -- not "does one coin move".
    """
    if panel.T < horizon_bars + min_obs:
        return {"available": False,
                "note": f"need {horizon_bars + min_obs} bars, have {panel.T}"}

    close = panel.close
    fwd = np.full(panel.T, np.nan)
    for t in range(panel.T - horizon_bars):
        r = close[t + horizon_bars] / close[t] - 1.0
        ok = np.isfinite(r)
        if ok.sum() >= 3:
            fwd[t] = float(np.nanmedian(r[ok]))     # median: one crazy coin should not define the hour

    hours, dows = _buckets(panel.ts, tz_offset_hours)
    valid = np.isfinite(fwd)

    def _test(labels: np.ndarray, names: list[str]) -> list[dict]:
        rows = []
        rest_pool = fwd[valid]
        for i, name in enumerate(names):
            m = valid & (labels == i)
            x = fwd[m]
            if x.size < min_obs:
                rows.append({"bucket": name, "n": int(x.size), "mean_bps": None,
                             "p_value": None, "sufficient": False})
                continue
            others = rest_pool[np.isfinite(rest_pool)]
            t_stat, p = sps.ttest_ind(x, others, equal_var=False)
            rows.append({
                "bucket": name, "n": int(x.size),
                "mean_bps": float(np.mean(x) * 1e4),
                "median_bps": float(np.median(x) * 1e4),
                "t_stat": float(t_stat), "p_value": float(p), "sufficient": True,
            })
        tested = [r for r in rows if r["p_value"] is not None]
        if tested:
            keep = benjamini_hochberg(np.array([r["p_value"] for r in tested]), alpha)
            for r, k in zip(tested, keep):
                r["survives_fdr"] = bool(k)
        for r in rows:
            r.setdefault("survives_fdr", False)
        return rows

    hour_rows = _test(hours, [f"{h:02d}:00" for h in range(24)])
    dow_rows = _test(dows, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])

    survivors_h = [r["bucket"] for r in hour_rows if r.get("survives_fdr")]
    survivors_d = [r["bucket"] for r in dow_rows if r.get("survives_fdr")]

    return {
        "available": True,
        "horizon_bars": horizon_bars,
        "timezone_offset_hours": tz_offset_hours,
        "fdr_alpha": alpha,
        "hour_of_day": hour_rows,
        "day_of_week": dow_rows,
        "hours_surviving_fdr": survivors_h,
        "days_surviving_fdr": survivors_d,
        "n_hypotheses_tested": len([r for r in hour_rows + dow_rows if r["p_value"] is not None]),
        "verdict": (
            f"{len(survivors_h)} hour buckets and {len(survivors_d)} weekday buckets survive "
            f"false-discovery-rate control at {alpha:.0%}."
            if (survivors_h or survivors_d) else
            "No calendar bucket survives multiple-testing correction. With 31 buckets tested, "
            "a couple of raw p-values below 0.05 are expected by chance alone and are not evidence."
        ),
        "warning": (
            "Calendar effects are the most over-discovered pattern in finance. Even a "
            "surviving bucket needs to hold out of sample before it earns capital."
        ),
    }
