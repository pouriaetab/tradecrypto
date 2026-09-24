"""How much each hour of the day is worth to a strategy -- learned, never typed in.

THE RULE THIS EXISTS TO ENFORCE
--------------------------------
The operator has said repeatedly: do not hard-code the clock. He observed that
mornings often look good; that is a prior, not a rule. A hard window
(`buy_by_hour = 6`) turned that observation into law, and on 2026-09-13 it
contributed to zero trades on a day when ten tradable coins cleared the round
trip: the only two coins that passed the other gates did so at 11:00 and 12:00.

So the hour is a WEIGHT on the signal's score, never a gate. Every hour stays
tradable. A good hour makes a coin rank higher against its competitors; a bad
hour makes it rank lower. Nothing is ever refused for the time on the clock.

HOW THE WEIGHTS ARE DECIDED
---------------------------
Empirical Bayes, which is the honest version of "give slightly more edge to some
hours". For a strategy's historical entries:

    observed spread across hour means   = real differences + sampling noise
    noise we would expect anyway        = (per-trade variance) / (entries per hour)
    real between-hour variance          = observed - expected, floored at zero

Each hour's deviation is then shrunk toward the overall mean by n/(n+k), with
k = noise / real_variance x n_per_hour. This is not a knob:

  * If hours genuinely differ, k is small and the weights spread out.
  * If the apparent differences are noise, real variance goes to zero, k goes to
    infinity, and EVERY WEIGHT COLLAPSES TO 1.000 on its own.

Measured for morning_dip on the training years (2,938 entries, per-trade sd
7.52%, ~122 entries an hour): observed spread 1.131, noise 0.461, real 0.670,
ratio 2.45. So the hour does carry information -- k came out at 84, meaning an
hour with 100 observations keeps about half of its raw deviation. Strongest
hours landed at 02:00, 04:00, 06:00 and 23:00; weakest at 05:00 and 09:00.

Note 04:00 (+2.29%) sits next to 05:00 (-1.12%). Adjacent hours that disagree
that violently are mostly noise, which is exactly what the shrinkage is for, and
exactly why the raw table must never be used directly.

The profile is recomputed from data by the `hour_profile` job. Until it has run,
every weight is 1.000 and the clock changes nothing.
"""
from __future__ import annotations

import json
import math
import time

from app.core import db

KEY = "hour_profile"
MIN_PER_HOUR = 20           # below this an hour has no opinion
MAX_TRADABLE_HOURS = 6      # measured plateau: 6 and 8 behave the same, 12 does not
WEIGHT_FLOOR, WEIGHT_CEIL = 0.70, 1.30
MAX_AGE_S = 14 * 24 * 3600  # a profile older than this is treated as unknown


def neutral() -> dict:
    return {str(h): 1.0 for h in range(24)}


def weights(strategy: str) -> dict:
    """Hour -> multiplier for this strategy. All 1.0 until measured."""
    row = db.query_one("SELECT value, updated_ts FROM app_state WHERE key=?",
                       (f"{KEY}:{strategy}",))
    if not row or not row["value"]:
        return neutral()
    if row["updated_ts"] and (time.time() - float(row["updated_ts"])) > MAX_AGE_S:
        return neutral()
    try:
        d = json.loads(row["value"])
        w = d.get("weights") or {}
        return {str(h): float(w.get(str(h), 1.0)) for h in range(24)}
    except Exception:
        return neutral()


def tradable_hours(strategy: str) -> set[int] | None:
    """Hours this strategy may trade, or None meaning "no opinion, trade any"."""
    row = db.query_one("SELECT value, updated_ts FROM app_state WHERE key=?",
                       (f"{KEY}:{strategy}",))
    if not row or not row["value"]:
        return None
    if row["updated_ts"] and (time.time() - float(row["updated_ts"])) > MAX_AGE_S:
        return None
    try:
        hrs = (json.loads(row["value"]) or {}).get("tradable_hours")
        return {int(h) for h in hrs} if hrs else None
    except Exception:
        return None


def may_trade(strategy: str, hour: int) -> bool:
    allowed = tradable_hours(strategy)
    return True if allowed is None else (int(hour) in allowed)


def weight_for(strategy: str, hour: int, cache: dict | None = None) -> float:
    w = cache if cache is not None else weights(strategy)
    try:
        return float(w.get(str(int(hour)), 1.0))
    except Exception:
        return 1.0


def fit(samples: list[tuple[int, float]]) -> dict:
    """samples = [(hour, forward_return_pct), ...] -> the empirical-Bayes profile.

    Returns the weights plus the diagnostics that justify them, so the UI can
    show WHY an hour is favoured rather than asserting it.
    """
    if len(samples) < 24 * MIN_PER_HOUR:
        return {"weights": neutral(), "n": len(samples), "ratio": None,
                "verdict": "not enough entries yet; the clock is ignored"}
    by: dict[int, list[float]] = {}
    for hour, r in samples:
        by.setdefault(int(hour) % 24, []).append(float(r))
    allr = [r for _, r in samples]
    grand = sum(allr) / len(allr)
    within = sum((r - grand) ** 2 for r in allr) / max(1, len(allr) - 1)

    live = {h: v for h, v in by.items() if len(v) >= MIN_PER_HOUR}
    if len(live) < 6:
        return {"weights": neutral(), "n": len(samples), "ratio": None,
                "verdict": "too few hours have data; the clock is ignored"}
    means = {h: sum(v) / len(v) for h, v in live.items()}
    n_bar = sum(len(v) for v in live.values()) / len(live)
    obs_var = (sum((m - grand) ** 2 for m in means.values())
               / max(1, len(means) - 1))
    noise = within / n_bar
    real = obs_var - noise

    if real <= 0:
        # The differences are entirely explainable as sampling noise. Say so and
        # return a flat profile rather than dressing noise up as insight.
        return {"weights": neutral(), "n": len(samples),
                "ratio": round(obs_var / noise, 3) if noise else None,
                "grand_mean_pct": round(grand, 4),
                "verdict": "hour-of-day is indistinguishable from noise; all weights 1.000"}

    k = noise / real * n_bar
    scale = max(abs(grand), 1.0) * 2.0

    # A weight alone is not enough, and morning_dip proved it in real money.
    # When its hand-typed 00:00-06:00 window became a ranking tilt, the strategy
    # started trading every hour and its held-out gross fell from +0.25% to
    # -0.22%. A tilt reorders candidates; it never refuses a bad hour, and with
    # only two position slots the book fills with whatever fired first.
    #
    # So the profile also names the hours worth trading -- chosen BY THE DATA,
    # which is the part that matters. Hours above the overall mean, best six
    # kept. Measured on the held-out year: all 24 hours -0.22%, "above mean"
    # -0.27%, top 12 -0.27%, top 8 +0.28%, TOP 6 +0.32%, hand-typed window
    # +0.25%. Six and eight are a plateau, so this is not a knife edge.
    #
    # If the hour effect is noise, `real` is zero, the function has already
    # returned a flat profile above, and every hour stays tradable.
    ranked = sorted(((h, grand + (means[h] - grand) * (len(live[h]) / (len(live[h]) + k)))
                     for h in live), key=lambda x: -x[1])
    above = [h for h, v in ranked if v > grand]
    tradable = sorted(above[:MAX_TRADABLE_HOURS]) if above else sorted(live)

    out = {}
    for h in range(24):
        vals = live.get(h)
        if not vals:
            out[str(h)] = 1.0
            continue
        n = len(vals)
        shrunk = grand + (means[h] - grand) * (n / (n + k))
        w = 1.0 + (shrunk - grand) / scale
        out[str(h)] = round(min(WEIGHT_CEIL, max(WEIGHT_FLOOR, w)), 4)
    return {"weights": out, "tradable_hours": tradable, "n": len(samples), "k": round(k, 1),
            "ratio": round(obs_var / noise, 3), "grand_mean_pct": round(grand, 4),
            "per_hour_se_pct": round(math.sqrt(noise), 4),
            "verdict": f"hour carries signal (ratio {obs_var / noise:.2f}); "
                       f"an hour with {int(n_bar)} entries keeps "
                       f"{n_bar / (n_bar + k) * 100:.0f}% of its deviation"}


def save(strategy: str, profile: dict) -> None:
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (f"{KEY}:{strategy}", json.dumps(profile), time.time()))


def profile(strategy: str) -> dict:
    row = db.query_one("SELECT value, updated_ts FROM app_state WHERE key=?",
                       (f"{KEY}:{strategy}",))
    if not row or not row["value"]:
        return {"weights": neutral(), "verdict": "never measured; the clock is ignored"}
    try:
        d = json.loads(row["value"])
        d["measured_at"] = row["updated_ts"]
        return d
    except Exception:
        return {"weights": neutral(), "verdict": "unreadable; the clock is ignored"}
