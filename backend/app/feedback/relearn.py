"""Feed real outcomes back into each strategy, every day, automatically.

A backtest is a hypothesis. Every closed trade is evidence against or for it, and
until now nothing compared the two: morning_dip was measured at -1.71% a trade
out of sample, then lost $18.64 over eight live paper trades while nothing in the
app noticed or acted.

What this does each day:

1. Scores every strategy on its OWN realised trades -- n, mean net, mean gross,
   what the spread took, win rate, median hold, and the same split by entry hour.
2. Compares that with what the backtest promised, and says whether live is worse
   than the hypothesis by more than sampling noise can explain.
3. PAUSES a strategy whose live evidence is bad enough, and un-pauses it when the
   evidence recovers. The pause is data, not a code edit, so the next run can
   reverse it.

The pause test is deliberately conservative, because with a handful of trades the
honest answer is almost always "not enough evidence yet":

    pause only if   n >= MIN_TRADES
              and   the 90% upper bound on the live mean net is still below zero
              and   the strategy is not the only one left running

The upper bound uses the t interval, so a wide spread of outcomes -- which is
what crypto produces -- makes it harder to condemn a strategy, not easier.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core import db

TZ = ZoneInfo("America/Chicago")
PAUSE_KEY = "paused_strategies"
MIN_TRADES = 12          # below this, no verdict either way
RESUME_UPPER = 0.0       # resume when the 90% upper bound climbs back above zero

# What each strategy's held-out backtest promised, in % net per trade at the
# 1.9182% round trip. Kept here so live results are compared against a number
# that was written down BEFORE the trades happened.
EXPECTED_NET_PCT = {
    "morning_dip": -1.71,
    "day_climb": -2.09,
    "pump_ride": -0.36,
    "volume_build": -0.94,
    "oversold_turn": +0.85,
}


def _t_crit(n: int) -> float:
    """Two-sided 90% t critical value; small-n table, normal above."""
    table = {2: 6.314, 3: 2.920, 4: 2.353, 5: 2.132, 6: 2.015, 7: 1.943, 8: 1.895,
             9: 1.860, 10: 1.833, 12: 1.796, 15: 1.761, 20: 1.729, 25: 1.711,
             30: 1.699, 40: 1.684, 60: 1.671, 120: 1.658}
    if n <= 1:
        return float("inf")
    # The table is keyed by SAMPLE SIZE, not degrees of freedom: table[2]=6.314
    # is t at df=1. Looking it up with `n - 1` fetched the value for a smaller
    # sample -- at n=3 it returned 6.314 instead of 2.920, 2.2x too wide. Every
    # interval was too wide, which biases this module toward "not enough
    # evidence" and makes it reluctant to pause a losing strategy.
    best = None
    for k in sorted(table):
        if n <= k:
            best = table[k]
            break
    return best if best is not None else 1.645


def paused() -> set[str]:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (PAUSE_KEY,))
    if not row or not row["value"]:
        return set()
    try:
        return set(json.loads(row["value"]) or [])
    except Exception:
        return set()


def _save_paused(names: set[str]) -> None:
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (PAUSE_KEY, json.dumps(sorted(names)), time.time()))


def score(strategy: str, mode: str = "paper") -> dict:
    """Everything the realised trades say about one strategy."""
    from app.feedback import defects as _def

    try:
        from app.research import versions as _versions
        _versions.ensure_schema()
    except Exception:
        _versions = None
    all_rows = [dict(r) for r in db.query(
        """SELECT strategy, entry_px, qty, holding_s, gross_pnl_usd, cost_usd,
                  net_pnl_usd, ts_open, ts_close, params_hash
           FROM trades WHERE strategy=? AND mode=? ORDER BY ts_close""", (strategy, mode))]
    # Trades our own bugs produced are kept in the ledger and dropped from the
    # SCORE. Judging a rule on a trade the rule did not choose is not evidence.
    rows = [r for r in all_rows if not _def.is_excluded(r)]
    # And trades from a superseded RULE VERSION score only as much as that
    # version resembled the one running (research/versions.py): a rewrite's
    # trades are history, a tuning's still count. Weight 0 drops the trade.
    try:
        _w = _versions.weights_for(strategy, rows) if _versions else None
    except Exception:
        _w = None
    n_discounted = 0
    if _w is not None:
        n_discounted = int(sum(1 for x in _w if x < 0.999))
        rows = [r for r, x in zip(rows, _w) if x > 0]
    out: dict = {"strategy": strategy, "n": len(rows),
                 "n_all": len(all_rows),
                 "n_excluded_defective": len(all_rows) - len([r for r in all_rows if not _def.is_excluded(r)]),
                 "n_from_earlier_versions": n_discounted,
                 "defects": _def.summary(all_rows)["by_defect"],
                 "expected_net_pct": EXPECTED_NET_PCT.get(strategy)}
    if not rows:
        out["verdict"] = (
            f"no clean trades yet — all {len(all_rows)} were produced under a known "
            f"defect and are excluded from scoring (they remain in P&L)"
            if all_rows else "no closed trades yet")
        return out

    # Percent per trade, so it is comparable with the backtest regardless of size.
    pcts, gross_pcts, nets = [], [], []
    by_hour: dict[int, list[float]] = {}
    for r in rows:
        notional = abs(float(r["entry_px"] or 0) * float(r["qty"] or 0))
        if notional <= 0:
            continue
        net_pct = float(r["net_pnl_usd"] or 0) / notional * 100.0
        pcts.append(net_pct)
        gross_pcts.append(float(r["gross_pnl_usd"] or 0) / notional * 100.0)
        nets.append(float(r["net_pnl_usd"] or 0))
        hr = datetime.fromtimestamp(float(r["ts_open"]), TZ).hour
        by_hour.setdefault(hr, []).append(net_pct)
    if not pcts:
        out["verdict"] = "trades exist but carry no notional"
        return out

    n = len(pcts)
    mean = statistics.fmean(pcts)
    sd = statistics.stdev(pcts) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    half = _t_crit(n) * se if n > 1 else float("inf")
    out.update({
        "mean_net_pct": round(mean, 4),
        "mean_gross_pct": round(statistics.fmean(gross_pcts), 4),
        "spread_took_pct": round(statistics.fmean(gross_pcts) - mean, 4),
        "sd_pct": round(sd, 4),
        "ci90": [round(mean - half, 4) if half != float("inf") else None,
                 round(mean + half, 4) if half != float("inf") else None],
        "win_rate_pct": round(sum(1 for x in pcts if x > 0) / n * 100, 1),
        "total_net_usd": round(sum(nets), 2),
        "median_hold_h": round(statistics.median(
            [float(r["holding_s"] or 0) / 3600 for r in rows]), 1),
        "by_hour": {str(h): round(statistics.fmean(v), 3) for h, v in sorted(by_hour.items())},
    })

    exp = out["expected_net_pct"]
    if n < MIN_TRADES:
        out["verdict"] = (f"{n} of {MIN_TRADES} trades — not enough evidence to judge "
                          f"(live {mean:+.2f}%, backtest said {exp:+.2f}%)"
                          if exp is not None else f"{n} of {MIN_TRADES} trades")
    elif out["ci90"][1] is not None and out["ci90"][1] < 0:
        out["verdict"] = (f"live {mean:+.2f}% a trade over {n}; the 90% interval tops out at "
                          f"{out['ci90'][1]:+.2f}%, still below zero")
    else:
        out["verdict"] = (f"live {mean:+.2f}% a trade over {n}; the 90% interval "
                          f"({out['ci90'][0]:+.2f}%, {out['ci90'][1]:+.2f}%) still includes zero")
    if exp is not None and n >= MIN_TRADES:
        out["vs_backtest_pp"] = round(mean - exp, 3)
    return out


def run(mode: str = "paper", apply_pauses: bool = True) -> dict:
    """Score everything, pause what the evidence condemns, resume what recovers."""
    from app.strategy.registry import studied

    cards = [score(s, mode) for s in studied()]
    was = paused()
    now = set(was)
    actions = []

    condemned = [c for c in cards
                 if c["n"] >= MIN_TRADES and (c.get("ci90") or [None, None])[1] is not None
                 and c["ci90"][1] < 0]
    # Never pause the last one standing: an empty book teaches nothing either.
    keepable = [c["strategy"] for c in cards if c["strategy"] not in now]
    for c in condemned:
        if c["strategy"] in now:
            continue
        if len(keepable) - 1 < 1:
            actions.append(f"{c['strategy']} would be paused but it is the last one running")
            continue
        now.add(c["strategy"])
        keepable.remove(c["strategy"])
        actions.append(f"paused {c['strategy']}: {c['verdict']}")

    for name in sorted(was):
        c = next((x for x in cards if x["strategy"] == name), None)
        if c and (c.get("ci90") or [None, None])[1] is not None and c["ci90"][1] > RESUME_UPPER:
            now.discard(name)
            actions.append(f"resumed {name}: {c['verdict']}")

    if apply_pauses and now != was:
        _save_paused(now)
        for a in actions:
            db.log_event("INFO", "relearn", a)

    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        ("strategy_scorecard", json.dumps({"as_of": time.time(), "cards": cards,
                                           "actions": actions}), time.time()))
    return {"cards": cards, "paused": sorted(now), "actions": actions}


def scorecard() -> dict:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", ("strategy_scorecard",))
    if not row or not row["value"]:
        return {"cards": [], "actions": [], "note": "not scored yet"}
    try:
        d = json.loads(row["value"])
        d["paused"] = sorted(paused())
        return d
    except Exception:
        return {"cards": [], "actions": []}
