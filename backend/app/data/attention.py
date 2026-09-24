"""Which coin is getting today's attention — and is its run already over?

The operator's observation, in his words: on any given day one or two coins move
a lot compared to everything else and take the attention for those hours (ARB and
UNI on the day he wrote this), and by the time he notices, the run is often done.

Both halves of that are measurable, and they are different questions:

  ATTENTION   is unusual activity happening here right now?
  EXHAUSTION  how much of the move is already behind us?

A coin worth trading scores high on the first and low on the second. A coin that
scores high on both is the trap he described — the thing you see precisely because
it already ran.

Attention score
---------------
Three standardised components, equally weighted, none of them price-direction:

    rvol    log(dollar volume today / trailing median dollar volume)
    range   today's true range / trailing ATR              (range expansion)
    rs      today's return − cross-sectional median return (relative strength)

Volume and range are deliberately weighted alongside return rather than behind
it. A coin up 9% on no volume is not getting attention; it is getting a print.

Exhaustion score
----------------
Four components, each in [0, 1], averaged:

    position_in_range   (price − low) / (high − low)   — 1.0 means at the highs
    range_used          today's range / trailing ATR, capped — how much of a
                        normal day's movement has already happened
    efficiency_decay    1 − (recent efficiency ratio / earlier efficiency ratio) —
                        the move losing its one-directional character
    time_of_day         fraction of the operator's trading day elapsed, but ONLY
                        applied when the calendar test says the hour actually
                        matters (see research/seasonality.py). His "runs dry up
                        around 4-5pm" belief is treated as a hypothesis, not a fact.

None of this is a strategy. It is a filter that ranks WHERE to look, and the
strategies still have to clear their own gates on whatever it surfaces.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel
from app.strategy.forced_momentum import efficiency_ratio


def _safe_z(x: np.ndarray) -> np.ndarray:
    ok = np.isfinite(x)
    if ok.sum() < 3:
        return np.full_like(x, np.nan)
    med = np.median(x[ok])
    mad = np.median(np.abs(x[ok] - med))
    scale = 1.4826 * mad
    if scale <= 0:
        return np.zeros_like(x)
    return (x - med) / scale


def attention(panel: Panel, t: int | None = None, day_bars: int = 1440,
              trail_days: int = 20) -> dict:
    """Rank the universe by how much unusual activity it is showing."""
    t = panel.T - 1 if t is None else t
    need = day_bars + 5
    if t < need:
        return {"available": False,
                "note": f"need {need} bars, have {t}; leave the engine running or backfill"}

    close = panel.close[: t + 1]
    high = panel.high[: t + 1] if panel.high is not None else close
    low = panel.low[: t + 1] if panel.low is not None else close
    vol = panel.volume[: t + 1] if panel.volume is not None else np.ones_like(close)

    day = slice(t + 1 - day_bars, t + 1)
    dv_today = np.nansum(vol[day] * close[day], axis=0)

    trail_start = max(0, t + 1 - day_bars * (trail_days + 1))
    dv_hist = []
    for d in range(1, trail_days + 1):
        a, b = t + 1 - day_bars * (d + 1), t + 1 - day_bars * d
        if a < trail_start or a < 0:
            break
        dv_hist.append(np.nansum(vol[a:b] * close[a:b], axis=0))
    dv_median = np.median(np.vstack(dv_hist), axis=0) if dv_hist else dv_today

    with np.errstate(divide="ignore", invalid="ignore"):
        rvol = np.log(np.maximum(dv_today, 1.0) / np.maximum(dv_median, 1.0))

    day_high = np.nanmax(high[day], axis=0)
    day_low = np.nanmin(low[day], axis=0)
    day_range = day_high - day_low
    atr = []
    for d in range(1, trail_days + 1):
        a, b = t + 1 - day_bars * (d + 1), t + 1 - day_bars * d
        if a < 0:
            break
        atr.append(np.nanmax(high[a:b], axis=0) - np.nanmin(low[a:b], axis=0))
    atr_med = np.median(np.vstack(atr), axis=0) if atr else day_range
    with np.errstate(divide="ignore", invalid="ignore"):
        range_exp = np.where(atr_med > 0, day_range / atr_med, np.nan)

    day_open = close[t + 1 - day_bars]
    with np.errstate(divide="ignore", invalid="ignore"):
        day_ret = np.where(day_open > 0, close[t] / day_open - 1.0, np.nan)
    rs = day_ret - np.nanmedian(day_ret)

    score = np.nanmean(np.vstack([_safe_z(rvol), _safe_z(range_exp), _safe_z(rs)]), axis=0)

    # ── exhaustion ───────────────────────────────────────────────────────────
    with np.errstate(divide="ignore", invalid="ignore"):
        pos_in_range = np.where(day_range > 0, (close[t] - day_low) / day_range, np.nan)
        range_used = np.clip(np.where(atr_med > 0, day_range / atr_med, np.nan) / 1.5, 0, 1)

    half = max(day_bars // 2, 30)
    er_recent = efficiency_ratio(close, t, half)
    er_early = efficiency_ratio(close, t - half, half) if t - half > half else er_recent
    with np.errstate(divide="ignore", invalid="ignore"):
        decay = np.clip(1.0 - np.abs(er_recent) / np.maximum(np.abs(er_early), 1e-6), 0, 1)

    exhaustion = np.nanmean(np.vstack([
        np.clip(pos_in_range, 0, 1), range_used, decay,
    ]), axis=0)

    rows = []
    for j, sym in enumerate(panel.symbols):
        rows.append({
            "symbol": sym,
            "attention_score": _f(score[j]),
            "components": {
                "relative_volume_log": _f(rvol[j]),
                "range_expansion": _f(range_exp[j]),
                "relative_strength_pct": _f(rs[j] * 100 if np.isfinite(rs[j]) else np.nan),
            },
            "day_return_pct": _f(day_ret[j] * 100 if np.isfinite(day_ret[j]) else np.nan),
            "dollar_volume_today": _f(dv_today[j]),
            "exhaustion_score": _f(exhaustion[j]),
            "exhaustion_components": {
                "position_in_day_range": _f(pos_in_range[j]),
                "range_used_vs_normal": _f(range_used[j]),
                "efficiency_decay": _f(decay[j]),
            },
            "verdict": _verdict(score[j], exhaustion[j]),
        })
    rows.sort(key=lambda r: (r["attention_score"] is None, -(r["attention_score"] or -99)))

    return {
        "available": True,
        "as_of_bar": t,
        "day_bars": day_bars,
        "trailing_days": trail_days,
        "rows": rows,
        "top": [r["symbol"] for r in rows[:3]],
        "tradeable_now": [r["symbol"] for r in rows
                          if r["verdict"] == "attention, run not obviously over"],
        "how_to_read": (
            "High attention with LOW exhaustion is the interesting quadrant. High "
            "attention with high exhaustion is the trap: the coin is visible because "
            "it already moved. This ranks where to look; it does not decide anything."
        ),
    }


def _verdict(score: float, exhaust: float) -> str:
    if not (np.isfinite(score) and np.isfinite(exhaust)):
        return "insufficient data"
    if score < 0.5:
        return "not in play"
    if exhaust > 0.7:
        return "attention, but the run looks late"
    if exhaust > 0.5:
        return "attention, mid-run"
    return "attention, run not obviously over"


def leadership_persistence(panel: Panel, day_bars: int = 1440, days: int = 20,
                           top_k: int = 3) -> dict:
    """Does today's leader tend to lead tomorrow?

    Directly testable, and it decides whether "trade whatever ran yesterday" is a
    strategy or a bias. Compares the observed rate of a top-K coin repeating
    against what independence would produce.
    """
    if panel.T < day_bars * (days + 1):
        return {"available": False,
                "note": f"need {day_bars * (days + 1)} bars, have {panel.T}"}
    leaders: list[set[str]] = []
    for d in range(days, 0, -1):
        t = panel.T - 1 - day_bars * (d - 1)
        if t >= panel.T or t < day_bars:
            continue
        a = t + 1 - day_bars
        ret = panel.close[t] / panel.close[a] - 1.0
        idx = np.argsort(np.where(np.isfinite(ret), ret, -np.inf))[::-1][:top_k]
        leaders.append({panel.symbols[i] for i in idx})

    if len(leaders) < 5:
        return {"available": False, "note": "not enough complete days"}
    repeats = sum(1 for a, b in zip(leaders, leaders[1:]) if a & b)
    trials = len(leaders) - 1
    rate = repeats / trials
    n = panel.N
    chance = 1 - (1 - top_k / n) ** top_k        # P(any overlap) under independence
    return {
        "available": True, "days_compared": trials, "top_k": top_k,
        "repeat_rate": rate, "rate_if_random": chance,
        "lift": rate / chance if chance > 0 else None,
        "verdict": (
            "yesterday's leaders repeat more often than chance — worth testing as a filter"
            if rate > chance * 1.5 else
            "leadership does not persist beyond chance; do not chase yesterday's mover"),
        "caveat": ("A small number of days makes this noisy. Treat it as a hint until "
                   "there are several months of history."),
    }


def _f(x) -> float | None:
    try:
        return float(x) if np.isfinite(x) else None
    except (TypeError, ValueError):
        return None
