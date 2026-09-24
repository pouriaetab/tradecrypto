"""Every signal that competed for a slot, and how the winner was scored.

2026-09-23, the operator: "for every signal that become a buy or sell order i
want ... to see what were there all the signals for that group ... competing to
become a order ... and all the metrics that was evaluated to eventually score
the one that was selected ... and behind each every decision i want to know what
factors/variable were include and with what weights and what algorithm."

The signals table already holds every candidate a strategy raised, taken or
rejected, with its features and its score. What was missing was the GROUPING --
a taken signal sitting in a list of 9,609 rows says nothing about what it beat.
A race is: one strategy, one bar, every candidate it saw, ranked.

SCORE_RECIPE is the other half: the actual expression each strategy ranks by,
copied from the code with its source line, plus what each input means. It is
deliberately the real formula and not a description of one -- a test asserts
every active strategy has an entry, so a new strategy cannot arrive without
saying how it scores.
"""
from __future__ import annotations

import json

from app.core import db

# The line each strategy ranks by, and what goes into it. `expr` is the literal
# expression from the strategy file.
SCORE_RECIPE: dict[str, dict] = {
    "day_climb": {
        "expr": "raw_score = climb_pct * hour_weight",
        "source": "strategy/day_climb.py",
        "plain": "How far the coin has already climbed over the last 4 hours, "
                 "multiplied by a weight for the hour of the day it is in Austin. "
                 "Nothing else enters the ranking — the volatility, the target and "
                 "the stop are computed after a signal wins, not to decide who wins.",
        "inputs": [
            {"name": "climb_pct", "weight": "x1 (linear)",
             "what": "percent the coin rose over the last 4 hours"},
            {"name": "hour_weight", "weight": "multiplier, learned per hour",
             "what": "how well this strategy has historically done entering at this "
                     "hour. It tilts the ranking; it never refuses a trade."},
        ],
        "gates_before_scoring": [
            "climb over 4h must be between climb_min_pct and climb_max_pct (5-15%)",
            "above pump_guard_pct (18%) it belongs to pump_ride, not here",
        ],
    },
    "volume_build": {
        "expr": "raw_score = volume_vs_normal",
        "source": "strategy/volume_build.py",
        "plain": "Purely how many times normal the coin's volume is. The climb, the "
                 "trend and the day filters are pass/fail gates before this — they "
                 "decide WHETHER a coin is eligible, not where it ranks.",
        "inputs": [
            {"name": "volume_vs_normal", "weight": "x1 (linear)",
             "what": "this hour's volume divided by its 336-hour baseline"},
        ],
        "gates_before_scoring": [
            "volume >= volume_multiple (1.5x) of baseline",
            "climbing >= climb_pct (3%) over climb_bars (2h)",
            "48h trend >= trend_min_pct (10%) or <= trend_oversold_pct (-10%)",
            "not already extended more than max_extended_pct (25%)",
        ],
    },
    "pump_catch": {
        "expr": "raw_score = volume_vs_normal",
        "source": "strategy/pump_catch.py (inherits volume_build)",
        "plain": "volume_build's ranking, on a rolling 60-minute window read every "
                 "15 minutes instead of the forming calendar hour.",
        "inputs": [
            {"name": "volume_vs_normal", "weight": "x1 (linear)",
             "what": "rolling 60-minute volume divided by its baseline"},
        ],
        "gates_before_scoring": ["the same gates as volume_build, on rolling windows"],
    },
    "morning_dip": {
        "expr": "raw_score = -dip_depth * hour_weight",
        "source": "strategy/morning_dip.py",
        "plain": "How deep the fall was, multiplied by an hour-of-day weight. Deeper "
                 "dip ranks higher.",
        "inputs": [
            {"name": "dip_depth", "weight": "x-1 (deeper ranks higher)",
             "what": "percent the coin fell over the lookback"},
            {"name": "hour_weight", "weight": "multiplier, learned per hour",
             "what": "how well entries at this Austin hour have done"},
        ],
        "gates_before_scoring": [
            "the fall must be at least dip_pct (10%)",
            "confirm_bars (3) of the last confirm_window (6) must confirm the turn",
        ],
    },
    "oversold_turn": {
        "expr": "raw_score = -drop_depth",
        "source": "strategy/oversold_turn.py",
        "plain": "How deep the hole is, and nothing else. The deepest drop ranks first.",
        "inputs": [
            {"name": "drop_depth", "weight": "x-1 (deeper ranks higher)",
             "what": "percent the coin fell over lookback_bars (24h)"},
        ],
        "gates_before_scoring": ["the fall must be at least drop_pct (20%)"],
    },
    "pump_ride": {
        "expr": "raw_score = pump_pct",
        "source": "strategy/pump_ride.py",
        "plain": "The size of the pump. Biggest mover ranks first. This strategy has "
                 "no target at all — the 8% trailing stop is its only exit.",
        "inputs": [
            {"name": "pump_pct", "weight": "x1 (linear)",
             "what": "percent the coin rose over pump_bars (4h)"},
        ],
        "gates_before_scoring": [
            "the rise must be at least pump_pct (18%)",
            "volume at least vol_mult of normal",
        ],
    },
}


def _checks(row) -> list[dict]:
    """The full gate chain stored on a signal. Older rows have none -- they say
    so rather than showing an empty list that reads like 'nothing was checked'."""
    try:
        raw = row["checks_json"]
    except Exception:
        raw = None
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def recipe(strategy: str) -> dict:
    """How this strategy ranks its candidates. Never invents one."""
    r = SCORE_RECIPE.get(strategy)
    if r:
        return dict(r, strategy=strategy, known=True)
    return {"strategy": strategy, "known": False,
            "expr": None, "source": None,
            "plain": "This strategy has no recorded scoring recipe. Its raw_score is "
                     "stored on every signal, but what goes into it is not written "
                     "down here — which is itself the finding.",
            "inputs": [], "gates_before_scoring": []}


def races(mode: str = "paper", limit: int = 40) -> list[dict]:
    """The most recent bars in which a signal was taken, each with its full field.

    Grouped on (strategy, ts): that is one strategy's view of one bar, which is
    the set that actually competed. Two strategies looking at the same minute are
    two separate races -- they never ranked against each other.
    """
    taken = db.query(
        "SELECT DISTINCT strategy, ts FROM signals WHERE decision LIKE 'taken%' "
        "ORDER BY ts DESC LIMIT ?", (int(limit),))
    out = []
    for t in taken:
        rows = db.query(
            "SELECT * FROM signals WHERE strategy=? AND ts=? ORDER BY raw_score DESC",
            (t["strategy"], t["ts"]))
        field = []
        for i, r in enumerate(rows, 1):
            try:
                feats = json.loads(r["features_json"] or "{}")
            except Exception:
                feats = {}
            field.append({
                "rank": i, "symbol": r["symbol"], "raw_score": r["raw_score"],
                "decision": r["decision"], "reject_reason": r["reject_reason"],
                "expected_edge_bps": r["expected_edge_bps"],
                "cost_hurdle_bps": r["cost_hurdle_bps"],
                "edge_ci_low_bps": r["edge_ci_low_bps"],
                "edge_ci_high_bps": r["edge_ci_high_bps"],
                "sample_size": r["sample_size"],
                "won": str(r["decision"] or "").startswith("taken"),
                "features": feats,
                # every gate that judged it, with its threshold and its reading
                "checks": _checks(r),
            })
        winners = [f for f in field if f["won"]]
        # 2026-09-23, measured: a bar where only ONE coin qualified returned
        # -3.11%/trade against +0.57% when two or more did (n=14 vs 58,
        # permutation p=0.007). The field size is therefore a FACT ABOUT THE
        # TRADE, not a curiosity about the page, so it is carried here.
        lone = len(field) == 1
        out.append({
            "strategy": t["strategy"], "ts": t["ts"],
            "candidates": len(field),
            "winners": [w["symbol"] for w in winners],
            "winner_rank": winners[0]["rank"] if winners else None,
            "top_score": field[0]["raw_score"] if field else None,
            "lone_candidate": lone,
            "recipe": recipe(t["strategy"]),
            "field": field,
        })
    return out
