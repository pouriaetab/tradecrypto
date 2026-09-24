"""Market regime and order-flow proxies.

Two of the operator's four strategy ideas depend on knowing "is the whole market
alive and moving up right now", and one depends on spotting large participants.
Both need to be measured rather than eyeballed, because both are exactly the kind
of thing that looks obvious in hindsight and is invisible in advance.

REGIME
------
Three independent readings, deliberately not blended into one magic score:

  breadth        fraction of the tradeable universe above its own N-bar moving
                 average. A market where 80% of coins are above trend is a
                 different animal from one where BTC alone is up.
  leader_trend   BTC's own state: above/below its long moving average, plus the
                 slope of that average. This is the "BTC woke up and went from
                 60k to 80k" reading, expressed as a number.
  dispersion     cross-sectional standard deviation of returns. High dispersion
                 means coins are moving independently (good for picking), low
                 dispersion means everything is one trade (good for riding,
                 bad for diversification -- your three positions are one position).

WHALE / FLOW PROXY -- read the caveat
-------------------------------------
We do NOT have order flow. Robinhood exposes none, and the public candle feed
gives volume, not trades. So there is no honest way to say "a whale bought here".

What we can measure is where volume is abnormal relative to the price move it
produced. Two standard, citable constructions:

  volume_z        volume relative to its own recent distribution
  amihud          |return| / dollar volume  (Amihud 2002 illiquidity)
  kyle_lambda     regression slope of return on signed volume (Kyle 1985 lambda)

Low Amihud with high volume means size moved through without moving price much,
which is what accumulation looks like. That is an INFERENCE from a proxy, not an
observation of a whale, and the model card says so. It stays flagged exploratory
until it demonstrates predictive value out of sample like anything else.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel


def moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if x.shape[0] < window:
        return np.full_like(x, np.nan)
    c = np.cumsum(np.insert(np.nan_to_num(x, nan=0.0), 0, 0.0, axis=0), axis=0)
    ma = (c[window:] - c[:-window]) / window
    pad = np.full((window, x.shape[1]) if x.ndim > 1 else (window,), np.nan)
    return np.concatenate([pad, ma], axis=0)[: x.shape[0]]


def breadth(panel: Panel, t: int, window: int = 120) -> dict:
    """Fraction of coins trading above their own moving average at time t."""
    if t < window + 1:
        return {"value": None, "n": 0, "note": f"need {window + 1} bars, have {t}"}
    ma = moving_average(panel.close[: t + 1], window)
    last, last_ma = panel.close[t], ma[t]
    ok = np.isfinite(last) & np.isfinite(last_ma)
    if ok.sum() < 4:
        return {"value": None, "n": int(ok.sum()), "note": "too few priceable coins"}
    above = (last[ok] > last_ma[ok]).mean()
    return {
        "value": float(above), "n": int(ok.sum()), "window_bars": window,
        "interpretation": (
            "risk-on" if above > 0.65 else "risk-off" if above < 0.35 else "mixed"
        ),
    }


def leader_trend(panel: Panel, t: int, leader: str = "BTC",
                 window: int = 200, slope_bars: int = 60) -> dict:
    """The leader coin's trend state and the slope of its own moving average."""
    if leader not in panel.symbols:
        return {"value": None, "note": f"{leader} not in the panel"}
    j = panel.symbols.index(leader)
    col = panel.close[: t + 1, j]
    if col.size < window + slope_bars + 1:
        return {"value": None, "note": f"need {window + slope_bars + 1} bars, have {col.size}"}
    ma = moving_average(col.reshape(-1, 1), window)[:, 0]
    if not np.isfinite(ma[-1]) or not np.isfinite(ma[-1 - slope_bars]):
        return {"value": None, "note": "moving average not yet defined"}
    # A flat or zero moving average (a coin with no prints yet, or a forward-
    # filled gap) divides by zero here and poisons the regime reading with nan.
    prev = ma[-1 - slope_bars]
    slope_bps = ((ma[-1] / prev - 1) * 1e4
                 if np.isfinite(prev) and abs(prev) > 1e-12 else float("nan"))
    above = bool(col[-1] > ma[-1])
    return {
        "leader": leader, "above_ma": above, "ma_slope_bps": float(slope_bps),
        "price": float(col[-1]), "ma": float(ma[-1]), "window_bars": window,
        "value": float(slope_bps),
        "interpretation": ("uptrend" if above and slope_bps > 0
                           else "downtrend" if not above and slope_bps < 0 else "transitional"),
    }


def dispersion(panel: Panel, t: int, window: int = 60) -> dict:
    """Cross-sectional spread of returns -- are coins moving as one, or apart?"""
    if t < window + 1:
        return {"value": None, "note": f"need {window + 1} bars"}
    r = panel.close[t] / panel.close[t - window] - 1.0
    ok = np.isfinite(r)
    if ok.sum() < 4:
        return {"value": None, "note": "too few coins"}
    return {
        "value": float(np.std(r[ok]) * 1e4), "median_return_bps": float(np.median(r[ok]) * 1e4),
        "n": int(ok.sum()), "window_bars": window,
        "interpretation": ("one trade -- positions are correlated, size down"
                           if np.std(r[ok]) * 1e4 < 100 else "coins moving independently"),
    }


def regime_report(panel: Panel, t: int | None = None) -> dict:
    t = panel.T - 1 if t is None else t
    if panel.T < 10:
        return {"available": False, "note": f"only {panel.T} bars of history"}
    b = breadth(panel, t)
    l = leader_trend(panel, t)
    d = dispersion(panel, t)
    risk_on = bool(b.get("value") is not None and b["value"] > 0.65
                   and l.get("above_ma") and (l.get("ma_slope_bps") or 0) > 0)
    return {
        "available": True,
        "breadth": b, "leader_trend": l, "dispersion": d,
        "risk_on": risk_on,
        "explanation": (
            "risk_on requires BOTH a broad market (over 65% of coins above trend) "
            "AND the leader in an uptrend. Either alone is not enough: a single "
            "coin running is not a regime, and breadth without the leader is "
            "usually a short-lived bounce."
        ),
    }


# ── flow proxies ──────────────────────────────────────────────────────────────
def flow_pressure(panel: Panel, t: int, window: int = 60) -> dict:
    """Per-coin volume anomaly and price impact. A PROXY, not order flow."""
    if panel.volume is None or t < window + 2:
        return {"available": False, "note": "need volume and enough history"}
    close = panel.close[: t + 1]
    vol = panel.volume[: t + 1]
    lr = np.diff(np.log(np.maximum(close[-(window + 1):], 1e-12)), axis=0)
    v = vol[-window:]
    dollar_v = v * close[-window:]

    with np.errstate(invalid="ignore", divide="ignore"):
        vmean, vsd = np.nanmean(v, axis=0), np.nanstd(v, axis=0)
        volume_z = np.where(vsd > 0, (v[-1] - vmean) / vsd, np.nan)
        amihud = np.nanmean(np.abs(lr) / np.maximum(dollar_v, 1.0), axis=0) * 1e9

    # Kyle's lambda: slope of |return| on dollar volume. A low slope with high
    # volume is size trading without moving price.
    lam = np.full(panel.N, np.nan)
    for j in range(panel.N):
        y, x = np.abs(lr[:, j]), dollar_v[:, j]
        m = np.isfinite(y) & np.isfinite(x) & (x > 0)
        if m.sum() > 10 and np.std(x[m]) > 0:
            lam[j] = float(np.polyfit(x[m], y[m], 1)[0] * 1e9)

    rows = []
    for j, sym in enumerate(panel.symbols):
        rows.append({
            "symbol": sym,
            "volume_z": _f(volume_z[j]),
            "amihud_illiquidity": _f(amihud[j]),
            "kyle_lambda": _f(lam[j]),
            "absorption": _f(volume_z[j] / lam[j]) if np.isfinite(lam[j]) and lam[j] > 0 else None,
        })
    rows.sort(key=lambda r: (r["absorption"] is None, -(r["absorption"] or 0)))
    return {
        "available": True, "window_bars": window, "rows": rows,
        "caveat": (
            "This is NOT whale detection. Robinhood exposes no order flow and the "
            "public feed gives volume, not individual trades. 'absorption' is high "
            "volume moving price unusually little, which is consistent with large "
            "passive accumulation and also consistent with several other things. "
            "It stays exploratory until it earns out-of-sample predictive value."
        ),
        "citations": [
            "Amihud, Y. (2002). Illiquidity and stock returns. J. Financial Markets 5(1).",
            "Kyle, A. (1985). Continuous Auctions and Insider Trading. Econometrica 53(6).",
        ],
    }


def _f(x) -> float | None:
    return float(x) if x is not None and np.isfinite(x) else None
