"""Are we buying the top? Lateness at entry, measured on every historical
entry a strategy would have made, against what happened next.

    "the pump started some around 10ish and other around early 11am ish but
     some ... we got in sort of late 11ish around for example 11:40ish almost
     at top for some of them, then some of those experienced drop"

Measured on 2026-09-20: the 11:00 and 12:00 day_climb entries were bought
3-10% above where the coin stood an hour earlier, within 1% of the two-hour
high, 30-90 minutes after the run began; their best gain afterwards was 0-2%
and most were down 1-5% two hours later. The 08:00 entries (AVAX, NEAR) were
bought +2.4-5% above the hour before and went on to +8-14%.

One day is an anecdote. This replays the strategy over its whole history and
buckets every entry by two lateness features knowable at the fill:

    last_hour_pct   how much of the move happened in the final hour before entry
    vs_high_pct     entry price against the highest high of the prior N hours
                    (0 = buying the high; negative = below it)

and reports the net outcome per bucket with n and a standard error, under the
strategy's OWN exit. If the late buckets are reliably worse by more than the
noise, the lateness feature becomes a candidate gate or a ranking input for
the retrainer -- fitted, not typed. If they are not, "we bought the top" was a
description of a bad day and not a rule.

No look-ahead: both features use bars at or before the signal bar.
"""
from __future__ import annotations

import json
import time

import numpy as np

from app.core import db

BUCKETS_LAST_HOUR = [(-1e9, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 8.0), (8.0, 1e9)]
BUCKETS_VS_HIGH = [(-1e9, -3.0), (-3.0, -1.0), (-1.0, 0.0), (0.0, 1e9)]
HIGH_WINDOW_H = 2


def _stats(v: list[float]) -> dict:
    a = np.asarray(v, dtype=float)
    a = a[np.isfinite(a)]
    n = int(a.size)
    if not n:
        return {"n": 0}
    return {"n": n, "mean_net_pct": float(a.mean()),
            "se_pct": float(a.std(ddof=1) / np.sqrt(n)) if n > 1 else 0.0,
            "win_rate": float((a > 0).mean() * 100.0),
            "median_net_pct": float(np.median(a))}


def study(strategy: str = "day_climb", days: int = 4 * 365, panel=None,
          cost_bps_per_side: float | None = None) -> dict:
    from app.execution import engine
    from app.research import backtest as bt
    from app.strategy.registry import build

    t0 = time.time()
    strat = build(strategy)
    if panel is None:
        panel = engine.build_panel(refresh=False, granularity=int(getattr(strat, "bar_seconds", 3600)),
                                   limit=days * int(86400 // int(getattr(strat, "bar_seconds", 3600))) + 400)
    if panel.T < strat.warmup_bars() + 100:
        return {"available": False, "why": f"only {panel.T} bars"}
    if cost_bps_per_side is None:
        try:
            from app.execution import rh_spread
            cost_bps_per_side = float(rh_spread.DEFAULT_SPREAD_PCT) * 100.0
        except Exception:
            cost_bps_per_side = 95.0

    res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps_per_side,
                          apply_hurdle=False, max_concurrent=10_000)
    bph = max(1, int(round(3600.0 / max(panel.bar_seconds(), 1.0))))
    close, high = panel.close, panel.high
    sym_idx = {s: i for i, s in enumerate(panel.symbols)}
    rows = []
    for tr in res.trades:
        t = tr.entry_i - 1                       # the signal bar (fill is one bar later)
        k = sym_idx[tr.symbol]
        if t - HIGH_WINDOW_H * bph - bph < 0:
            continue
        px = close[t, k]
        prev_h = close[t - bph, k]
        hi = np.nanmax(high[t - HIGH_WINDOW_H * bph:t, k]) if high is not None else np.nan
        if not (np.isfinite(px) and np.isfinite(prev_h) and prev_h > 0 and np.isfinite(hi) and hi > 0):
            continue
        rows.append({"symbol": tr.symbol, "t": int(t),
                     "last_hour_pct": float((px / prev_h - 1.0) * 100.0),
                     "vs_high_pct": float((px / hi - 1.0) * 100.0),
                     "net_pct": float(tr.net_ret * 100.0), "reason": tr.exit_reason})

    def bucketed(key, edges):
        out = []
        for lo, hi_ in edges:
            v = [r["net_pct"] for r in rows if lo <= r[key] < hi_]
            label = (f"< {hi_:g}%" if lo < -1e8 else f">= {lo:g}%" if hi_ > 1e8 else f"{lo:g} to {hi_:g}%")
            out.append({"bucket": label, **_stats(v)})
        return out

    all_stats = _stats([r["net_pct"] for r in rows])
    by_last_hour = bucketed("last_hour_pct", BUCKETS_LAST_HOUR)
    by_vs_high = bucketed("vs_high_pct", BUCKETS_VS_HIGH)

    # Is lateness informative at all? Spearman rank correlation with a
    # permutation p-value, so a single fat bucket cannot fake a trend.
    def spearman(x, y):
        x, y = np.asarray(x), np.asarray(y)
        rx, ry = x.argsort().argsort().astype(float), y.argsort().argsort().astype(float)
        rx -= rx.mean(); ry -= ry.mean()
        d = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
        return float((rx * ry).sum() / d) if d else 0.0

    verdicts = {}
    rng = np.random.default_rng(0)
    y = [r["net_pct"] for r in rows]
    for key in ("last_hour_pct", "vs_high_pct"):
        x = [r[key] for r in rows]
        if len(x) < 50:
            verdicts[key] = {"rho": None, "p_value": None, "note": "fewer than 50 entries"}
            continue
        rho = spearman(x, y)
        perm = np.array([spearman(x, rng.permutation(y)) for _ in range(400)])
        p = float((np.abs(perm) >= abs(rho)).mean())
        verdicts[key] = {"rho": rho, "p_value": p,
                         "note": ("later entries do worse — worth fitting as a ranking input"
                                  if rho < 0 and p < 0.05 else
                                  "later entries do better — the opposite of the worry"
                                  if rho > 0 and p < 0.05 else
                                  "no reliable relation between lateness and outcome")}

    return {"available": True, "strategy": strategy, "n_entries": len(rows),
            "span_days": float((panel.ts[-1] - panel.ts[0]) / 86400.0),
            "all": all_stats, "by_last_hour_move": by_last_hour,
            "by_distance_from_high": by_vs_high, "verdicts": verdicts,
            "cost_bps_per_side": cost_bps_per_side, "took_s": time.time() - t0,
            "definition": (f"last_hour_pct = signal-bar close vs the close one hour earlier; "
                           f"vs_high_pct = signal-bar close vs the highest high of the prior "
                           f"{HIGH_WINDOW_H} hours. Fill one bar after the signal, the strategy's "
                           f"own exit, {cost_bps_per_side:.0f} bps charged each side.")}


def record(res: dict) -> None:
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "entry_lateness", res.get("strategy", "?"),
                json.dumps({"high_window_h": HIGH_WINDOW_H}), json.dumps(res),
                "; ".join(f"{k}: {v.get('note', '')}" for k, v in res.get("verdicts", {}).items())[:400]))


def latest(strategy: str | None = None) -> dict | None:
    q = "SELECT ts, result_json FROM runs WHERE kind='entry_lateness'"
    args: tuple = ()
    if strategy:
        q += " AND label=?"; args = (strategy,)
    row = db.query_one(q + " ORDER BY ts DESC LIMIT 1", args)
    if not row:
        return None
    d = json.loads(row["result_json"]); d["ran_at"] = row["ts"]
    return d
