"""coin_stacking: what a SECOND strategy in an already-held coin would have made.

WHAT THIS IS
------------
Two strategies holding the same coin at once used to be allowed, on the
reasoning that a pump rule and a climb rule are different ideas about the same
instrument. Paused in the book on 2026-09-22; kept alive here.

The operator's argument was structural, and it is a good one: on the day he
raised it, CHIP was held by volume_build and pump_catch entered in the SAME
MINUTE and SHIB five minutes apart -- $476 of a ~$2,000 book doubled up in four
coins. Two rules firing on one coin inside five minutes are one idea bought
twice, paying the round trip twice for it.

THE EVIDENCE, STATED HONESTLY
-----------------------------
It did not agree with him. Three stacked trades had closed and they made
**+$5.36**, +$1.79 a trade against +$0.65 for everything else. Three trades
decide nothing either way, which is exactly why this module exists rather than
the behaviour simply being deleted: the block records every refusal with its
full features, and this replays them.

If the blocked entries turn out to have been good, the evidence brings it back.
If they turn out to have been the same idea at double cost, that is settled with
numbers instead of an argument.

WHAT THE REPLAY DOES
--------------------
For each refused entry: walk the coin's own bars forward from the signal, apply
the exit the signal itself asked for (its target, its stop, its hold), and
charge Robinhood's measured round trip for that coin. That is the same shape
`exit_lab` uses on real trades, applied to trades that were never placed.

WHAT IT CANNOT SAY
------------------
It assumes the entry filled at the bar's close and that the exit fills at the
target or stop the moment it is touched. Both flatter the result slightly. A
verdict is only offered once there are enough refusals to mean anything.
"""
from __future__ import annotations

import json
import time

from app.core import db

REASON_MARK = "coin_stacking"
MIN_FOR_VERDICT = 25


def _spread_frac(symbol: str) -> float:
    try:
        from app.execution import rh_spread
        return float(rh_spread.get(symbol).get("spread_pct") or 0.0) / 100.0
    except Exception:
        return 0.0095


def _bars(symbol: str, since: float, until: float) -> list[dict]:
    for g in (900, 3600):
        try:
            rows = db.query(
                "SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=? "
                "AND ts >= ? AND ts <= ? ORDER BY ts",
                (symbol, g, int(since), int(until)))
        except Exception:
            return []
        if len(rows) >= 2:
            return [dict(r) for r in rows]
    return []


def _replay_one(sig: dict) -> dict | None:
    """What this refused entry would have returned, net of both sides."""
    try:
        f = json.loads(sig["features_json"] or "{}")
    except Exception:
        f = {}
    tgt = float(f.get("target_bps") or 0.0) / 1e4
    stop = float(f.get("stop_bps") or 0.0) / 1e4
    hold = float(f.get("hold_seconds") or 0.0)
    if tgt <= 0 or stop <= 0 or hold <= 0:
        return None
    t0 = float(sig["ts"])
    bars = _bars(sig["symbol"], t0, t0 + hold)
    if not bars:
        return None
    entry = float(bars[0]["close"] or 0.0)
    if entry <= 0:
        return None
    side = _spread_frac(sig["symbol"])
    round_trip = 2.0 * side
    out_pct, why, age = None, "ran out of time", hold
    for b in bars[1:]:
        hi = float(b["high"] or 0.0) / entry - 1.0
        lo = float(b["low"] or 0.0) / entry - 1.0
        # The stop is checked FIRST. Within one bar both can be touched and we
        # cannot know the order; assuming the good one happened first is how a
        # replay flatters itself.
        if lo <= -stop:
            out_pct, why, age = -stop, "stop", float(b["ts"]) - t0
            break
        if hi >= tgt:
            out_pct, why, age = tgt, "target", float(b["ts"]) - t0
            break
    if out_pct is None:
        out_pct = float(bars[-1]["close"] or entry) / entry - 1.0
        age = float(bars[-1]["ts"]) - t0
    return {"symbol": sig["symbol"], "strategy": sig["strategy"], "ts": t0,
            "gross_pct": out_pct * 100.0,
            "net_pct": (out_pct - round_trip) * 100.0,
            "round_trip_pct": round_trip * 100.0,
            "exit": why, "held_s": age}


def study(days: float = 30.0) -> dict:
    """Every entry the coin_stacking block refused, and what it would have done."""
    since = time.time() - days * 86400.0
    try:
        sigs = db.query(
            "SELECT ts, strategy, symbol, features_json FROM signals "
            "WHERE ts >= ? AND reject_reason LIKE ? ORDER BY ts",
            (since, f"%{REASON_MARK}%"))
    except Exception as exc:
        return {"available": False, "why": f"could not read signals: {exc}"}
    refused = len(sigs)
    replayed = [r for r in (_replay_one(dict(s)) for s in sigs) if r]
    if not replayed:
        return {"available": False, "refused": refused, "replayed": 0,
                "why": (f"{refused} entr{'y' if refused == 1 else 'ies'} refused so far; "
                        f"none has enough stored bars to replay yet"
                        if refused else
                        "nothing has been refused yet — the block was added 2026-09-22")}
    import statistics as st
    nets = [r["net_pct"] for r in replayed]
    mean = st.mean(nets)
    se = (st.pstdev(nets) / (len(nets) ** 0.5)) if len(nets) > 1 else 0.0
    n = len(nets)
    verdict = (
        f"not enough yet — {n} of {MIN_FOR_VERDICT} replays needed before this means anything"
        if n < MIN_FOR_VERDICT else
        f"the blocked entries would have made {mean:+.2f}% a trade net of costs "
        f"({'worth reconsidering' if mean - 2 * se > 0 else 'the block is not costing anything'})")
    return {
        "available": True, "refused": refused, "replayed": n,
        "mean_net_pct": mean, "se_pct": se,
        "win_rate_pct": 100.0 * sum(1 for x in nets if x > 0) / n,
        "by_exit": {k: sum(1 for r in replayed if r["exit"] == k)
                    for k in ("target", "stop", "ran out of time")},
        "verdict": verdict,
        "trades": replayed[-50:],
        "note": ("Entry assumed at the signal bar's close and exit the moment the "
                 "target or stop is touched; the stop is tested first within a bar. "
                 "Both simplifications flatter the result slightly."),
        "generated_at": time.time(),
    }
