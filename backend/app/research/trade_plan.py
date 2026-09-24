"""The plan a trade was taken on, written down at entry, and graded as it runs.

2026-09-23, the operator: "each trade entry with its related strategy should
have a definite clear transparent plan that i can see with details on why the
trade was taken and out of all signals this one was promoted, then what was the
prediction for price ... and what is the expected movement ... then if the
behaviour changed ... we should have tagged this even live as a possibly failed
signal before 4 hrs and then after 4 hrs as a failed wrong prediction."

So every entry writes a PLAN: what we expect, by when, how likely, what the path
should look like, and why this signal beat the others. Then the plan is graded
against reality on every tick, and the grade is a durable record -- not a
feeling, and not something reconstructed afterwards from the price.

THE POINT OF WRITING IT AT ENTRY is that a prediction you can still edit is not
a prediction. The row is inserted once, before the outcome exists.

The four states, in his words:

  on_track          doing roughly what we said
  wobbling          "possibly failed signal" -- inside the window, but the path
                    has already broken the shape we predicted
  failed            past the window without reaching the target: the prediction
                    was wrong, full stop
  exit_seeking      failed AND we have seen a moment worth taking -- this is the
                    hour-15 blip he described, made into a state instead of a
                    missed opportunity

The shape table is measured, not asserted: 283k windows, 71 coins, four years,
each entry graded over ITS OWN predicted window (see session 69 for where the
window comes from).
"""
from __future__ import annotations

import json
import time

from app.core import db
from app.strategy.time_budget import budget

# How the path to the target actually looks, by ratio bucket. Measured
# 2026-09-23 on 283,322 windows. Read: of every 100 entries at this ratio, how
# many climbed with barely a dip, dipped a little first, dropped hard then
# recovered, or never got there inside the window -- and the median worst dip.
SHAPE_TABLE: dict[int, dict] = {
    0:  {"lo": 0.0,  "straight": 19, "small_dip": 24, "hard_dip": 8, "miss": 48, "median_dip": -3.4},
    1:  {"lo": 1.5,  "straight": 18, "small_dip": 19, "hard_dip": 4, "miss": 59, "median_dip": -3.1},
    2:  {"lo": 2.5,  "straight": 15, "small_dip": 15, "hard_dip": 2, "miss": 67, "median_dip": -3.0},
    3:  {"lo": 3.5,  "straight": 13, "small_dip": 11, "hard_dip": 1, "miss": 75, "median_dip": -2.9},
    4:  {"lo": 5.0,  "straight": 10, "small_dip":  7, "hard_dip": 1, "miss": 82, "median_dip": -2.7},
    5:  {"lo": 7.0,  "straight":  7, "small_dip":  4, "hard_dip": 0, "miss": 88, "median_dip": -2.4},
    6:  {"lo": 10.0, "straight":  5, "small_dip":  2, "hard_dip": 0, "miss": 92, "median_dip": -2.0},
    7:  {"lo": 14.0, "straight":  3, "small_dip":  1, "hard_dip": 0, "miss": 96, "median_dip": -1.4},
}

# A trade that has already fallen further than the typical path ever does has
# broken its own prediction, even though its window has not run out. 1.8x the
# median dip for its bucket -- a multiplier, so it scales with the shape rather
# than being one percentage pinned across every coin.
WOBBLE_K = 1.8

# THE RECOVERY EXIT. 2026-09-23, the operator, reading the high & low tab:
# "when realized we got lucky after bad predicted entry ... we can do breakeven
# or little bit profit to just exit the position, but we didnt even though the
# worst came before the best."
#
# He is right, and the shipped rules could not have helped: on all 9 open
# positions that were ever green, the green arrived INSIDE the window, so
# `exit_seeking` (which requires the window to have expired) never fired once.
#
# So this fires on the DIP, not the clock. Once a trade has fallen further than
# 1.3x the dip its shape normally takes, the entry was wrong; the first time it
# climbs back to RECOVER_GAIN_PCT, take it.
#
# Replayed on 59 closed trades: mean -0.57% -> -0.28% (p = 0.26, so NOT a
# proven gain), but volatility 4.93% -> 3.65%, win rate 53% -> 78%, and trades
# losing more than 5% fall from 24% to 14%. It is a DE-RISKING rule, not a
# profit rule, and it is shipped as one. The cost is the upside tail: the best
# outcome falls from +10.64% to +7.48%.
RECOVER_DIP_K = 1.3
RECOVER_GAIN_PCT = 0.50


def _bucket(ratio: float | None) -> dict:
    if ratio is None:
        return SHAPE_TABLE[7]
    row = SHAPE_TABLE[0]
    for b in SHAPE_TABLE.values():
        if ratio >= b["lo"]:
            row = b
    return row


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS trade_plans (
        id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL, strategy TEXT NOT NULL, mode TEXT NOT NULL,
        opened_ts REAL NOT NULL,
        entry_px REAL, target_px REAL, stop_px REAL,
        target_frac REAL, atr_frac REAL, ratio REAL,
        p_reach REAL, window_h REAL,
        expected_dip_pct REAL, shape_json TEXT, why_json TEXT,
        state TEXT NOT NULL DEFAULT 'on_track',
        state_ts REAL, worst_pct REAL, best_pct REAL,
        notes_json TEXT)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS trade_plans_pos "
               "ON trade_plans(symbol, strategy, mode, opened_ts)")


def build(symbol: str, strategy: str, entry_px: float, target_px: float | None,
          stop_px: float | None, atr_frac: float, why: dict | None = None) -> dict:
    """The plan, as a plain dict. Pure -- no database, no clock."""
    target_frac = (float(target_px) / float(entry_px) - 1.0) if (target_px and entry_px) else None
    b = budget(target_frac, atr_frac) if target_frac else {
        "ratio": None, "p_reach": 0.0, "p50_h": 0, "p70_h": 0,
        "why": "this strategy runs without a target"}
    sh = _bucket(b["ratio"])
    return {
        "symbol": symbol, "strategy": strategy,
        "entry_px": float(entry_px),
        "target_px": float(target_px) if target_px else None,
        "stop_px": float(stop_px) if stop_px else None,
        "target_frac": target_frac,
        "atr_frac": float(atr_frac or 0.0),
        "ratio": b["ratio"],
        "p_reach": b["p_reach"],
        "window_h": b["p70_h"],
        "expected_dip_pct": sh["median_dip"],
        "shape": {"straight_up_pct": sh["straight"],
                  "small_dip_pct": sh["small_dip"],
                  "hard_dip_pct": sh["hard_dip"],
                  "never_reached_pct": sh["miss"]},
        "why": why or {},
        "prediction": (
            f"{symbol} at {entry_px:.6g} should reach {target_px:.6g} "
            f"(+{target_frac*100:.2f}%) within {b['p70_h']}h."
            if target_frac else f"{symbol} runs with no target; the trail is the exit."),
        "basis": b["why"],
        "how_measured": (
            "The window and the odds come from 283,206 entry windows over 71 coins "
            "and four years of hourly bars, bucketed by target size divided by the "
            "coin's own average hourly range, and checked on a 2025-26 holdout the "
            "fit never saw. The shape row is the same windows graded on the path "
            "they took to get there."),
    }


def wobble_floor_pct(plan: dict) -> float:
    """How far down is still 'normal' for this plan before we call it broken."""
    return float(plan.get("expected_dip_pct") or -3.0) * WOBBLE_K


def grade(plan: dict, age_h: float, worst_pct: float, best_pct: float,
          now_pct: float, reached: bool) -> tuple[str, str]:
    """The live verdict on a plan. Returns (state, one plain sentence).

    Deliberately ordered worst-first: a trade can be both past its window and
    deeply under water, and 'failed' is the more useful thing to say.
    """
    if reached:
        return "on_track", "reached the price we predicted"
    # The recovery exit comes FIRST, because it is the only state that can fire
    # while the window is still open -- which is where every missed chance on
    # the live book actually was.
    if (worst_pct <= float(plan.get("expected_dip_pct") or -3.0) * RECOVER_DIP_K
            and now_pct >= RECOVER_GAIN_PCT):
        return ("recovered",
                f"fell {worst_pct:.2f}% when this shape dips about "
                f"{plan.get('expected_dip_pct'):.1f}% — the entry was wrong. Back to "
                f"{now_pct:+.2f}%, so take it and move on")
    floor = wobble_floor_pct(plan)
    win = float(plan.get("window_h") or 0)
    if age_h > win:
        if now_pct >= 0:
            return ("exit_seeking",
                    f"the prediction was wrong — {age_h:.1f}h against a {win:g}h window — "
                    f"but it is {now_pct:+.2f}% now, which is a way out")
        return ("failed",
                f"the prediction was wrong: {age_h:.1f}h against a {win:g}h window, "
                f"still {now_pct:+.2f}%")
    if worst_pct <= floor:
        return ("wobbling",
                f"already down {worst_pct:.2f}% when this shape typically dips "
                f"{plan.get('expected_dip_pct'):.1f}% — the path has broken early")
    return "on_track", f"inside its {win:g}h window, {now_pct:+.2f}% so far"


def record(plan: dict, mode: str, opened_ts: float) -> None:
    """Write the plan once, at entry. Never updated -- see the module docstring."""
    ensure_schema()
    db.execute(
        """INSERT OR IGNORE INTO trade_plans
           (symbol, strategy, mode, opened_ts, entry_px, target_px, stop_px,
            target_frac, atr_frac, ratio, p_reach, window_h, expected_dip_pct,
            shape_json, why_json, state, state_ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'on_track',?)""",
        (plan["symbol"], plan["strategy"], mode, float(opened_ts),
         plan["entry_px"], plan["target_px"], plan["stop_px"],
         plan["target_frac"], plan["atr_frac"], plan["ratio"], plan["p_reach"],
         plan["window_h"], plan["expected_dip_pct"],
         json.dumps(plan["shape"]), json.dumps(plan["why"]), time.time()))


def mark(symbol: str, strategy: str, mode: str, opened_ts: float, state: str,
         note: str, worst_pct: float, best_pct: float) -> bool:
    """Move a plan to a new state. Returns True only on an actual change, so the
    caller can log once instead of once per tick."""
    ensure_schema()
    row = db.query_one(
        "SELECT state, notes_json FROM trade_plans WHERE symbol=? AND strategy=? "
        "AND mode=? AND opened_ts=?", (symbol, strategy, mode, float(opened_ts)))
    if not row:
        return False
    db.execute("UPDATE trade_plans SET worst_pct=?, best_pct=? WHERE symbol=? AND "
               "strategy=? AND mode=? AND opened_ts=?",
               (worst_pct, best_pct, symbol, strategy, mode, float(opened_ts)))
    if row["state"] == state:
        return False
    notes = []
    try:
        notes = json.loads(row["notes_json"] or "[]")
    except Exception:
        notes = []
    notes.append({"ts": time.time(), "state": state, "note": note})
    db.execute("UPDATE trade_plans SET state=?, state_ts=?, notes_json=? WHERE symbol=? "
               "AND strategy=? AND mode=? AND opened_ts=?",
               (state, time.time(), json.dumps(notes[-20:]), symbol, strategy,
                mode, float(opened_ts)))
    return True


def live(mode: str = "paper", limit: int = 200) -> list[dict]:
    """Every plan, newest first, with its notes unpacked for the page."""
    ensure_schema()
    out = []
    for r in db.query("SELECT * FROM trade_plans WHERE mode=? ORDER BY opened_ts DESC "
                      "LIMIT ?", (mode, int(limit))):
        d = dict(r)
        for k, dflt in (("shape_json", {}), ("why_json", {}), ("notes_json", [])):
            try:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or ("[]" if dflt == [] else "{}"))
            except Exception:
                d[k.replace("_json", "")] = dflt
        out.append(d)
    return out


# ── BACKFILL ─────────────────────────────────────────────────────────────────
# 2026-09-23, the operator: "can we do this same thing for all the past trades
# or it is too late?"
#
# Not too late. Everything a plan needs is either causal or still stored:
#
#   the target        -- on the trade row, as booked
#   the coin's range  -- from the 24 hourly bars BEFORE the fill, never after
#   the window/odds   -- functions of those two, same table as the live path
#   the outcome       -- replayed from the bars the trade actually lived through
#   why this signal   -- the signals table, where a matching row exists
#
# The one thing that is NOT reconstructible is honesty about authorship, so
# every backfilled row carries reconstructed: true and says what was rebuilt.
# A reconstructed plan is evidence about the STRATEGY; it is not evidence that
# anyone predicted anything in advance.

BACKFILL_MATCH_S = 1800.0        # how close a signal must sit to the fill


def _range_before(symbol: str, ts: float) -> float:
    rows = db.query(
        "SELECT high, low, close FROM bars WHERE symbol=? AND granularity=3600 "
        "AND ts < ? AND close IS NOT NULL AND close > 0 ORDER BY ts DESC LIMIT 24",
        (symbol, float(ts)))
    rng = [(float(r["high"]) - float(r["low"])) / float(r["close"])
           for r in rows if r["high"] and r["low"] and r["close"]]
    return sum(rng) / len(rng) if rng else 0.0


def _why_from_signals(symbol: str, strategy: str, ts: float) -> dict:
    """The signal that became this trade, and how it ranked among its peers."""
    row = db.query_one(
        "SELECT * FROM signals WHERE symbol=? AND strategy=? AND decision LIKE 'taken%' "
        "AND ABS(ts - ?) < ? ORDER BY ABS(ts - ?) LIMIT 1",
        (symbol, strategy, float(ts), BACKFILL_MATCH_S, float(ts)))
    if not row:
        return {"reconstructed": True, "signal_found": False,
                "note": "no signal row within 30 minutes of this fill — this trade "
                        "predates signal logging, so why it beat the others is lost"}
    peers = db.query(
        "SELECT raw_score FROM signals WHERE strategy=? AND ABS(ts - ?) < 1 ",
        (strategy, float(row["ts"])))
    scores = [float(p["raw_score"]) for p in peers if p["raw_score"] is not None]
    try:
        feats = json.loads(row["features_json"] or "{}")
    except Exception:
        feats = {}
    return {"reconstructed": True, "signal_found": True,
            "raw_score": float(row["raw_score"] or 0.0),
            "expected_edge_bps": float(row["expected_edge_bps"] or 0.0),
            "cost_hurdle_bps": float(row["cost_hurdle_bps"] or 0.0),
            "decision": row["decision"],
            "signals_this_bar": len(scores),
            "rank_this_bar": 1 + sum(1 for x in scores if x > float(row["raw_score"] or 0.0)),
            "features": feats}


def _replay_outcome(symbol: str, t0: float, t1: float, entry: float,
                    target_px: float | None, window_h: float) -> dict:
    """What actually happened, from the bars the trade lived through."""
    try:
        from app.execution import rh_spread as _rs
        side = float(_rs.get(symbol)["spread_pct"]) / 100.0
    except Exception:
        side = 0.0095
    bars = db.query(
        "SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=3600 "
        "AND ts >= ? AND ts <= ? AND close IS NOT NULL ORDER BY ts",
        (symbol, float(t0), float(t1)))
    if not bars:
        return {}
    net = lambda px: (float(px) * (1 - side) / entry - 1.0) * 100.0
    worst = best = net(bars[0]["close"])
    hit_h = None
    worst_in_window = 0.0
    for b in bars:
        age_h = (float(b["ts"]) - t0) / 3600.0
        if b["high"]:
            best = max(best, net(b["high"]))
            if target_px and hit_h is None and float(b["high"]) >= float(target_px):
                hit_h = age_h
        if b["low"]:
            worst = min(worst, net(b["low"]))
            if age_h <= window_h:
                worst_in_window = min(worst_in_window, net(b["low"]))
    return {"worst_pct": worst, "best_pct": best, "hit_h": hit_h,
            "worst_in_window_pct": worst_in_window,
            "final_pct": net(bars[-1]["close"]),
            "lived_h": (float(bars[-1]["ts"]) - t0) / 3600.0}


def backfill(mode: str = "paper", limit: int = 500) -> dict:
    """Rebuild a plan for every closed trade that does not have one."""
    ensure_schema()
    have = {(r["symbol"], r["strategy"], round(float(r["opened_ts"]), 3))
            for r in db.query("SELECT symbol, strategy, opened_ts FROM trade_plans "
                              "WHERE mode=?", (mode,))}
    made = skipped = 0
    verdicts: dict[str, int] = {}
    for t in db.query(
            "SELECT id, symbol, strategy, ts_open, ts_close, entry_px, exit_px "
            "FROM trades WHERE mode=? AND ts_close IS NOT NULL AND entry_px > 0 "
            "ORDER BY ts_open DESC LIMIT ?", (mode, int(limit))):
        key = (t["symbol"], t["strategy"], round(float(t["ts_open"]), 3))
        if key in have:
            skipped += 1
            continue
        atr = _range_before(t["symbol"], float(t["ts_open"]))
        # The target as the desk actually set it is not on the trade row, so it
        # is taken from the signal's stored target_bps where one exists, and
        # otherwise from the strategy's own default -- both are entry-time facts.
        why = _why_from_signals(t["symbol"], t["strategy"], float(t["ts_open"]))
        tb = (why.get("features") or {}).get("target_bps")
        target_px = (float(t["entry_px"]) * (1 + float(tb) / 1e4)
                     if tb and 0 < float(tb) < 90_000 else None)
        sb = (why.get("features") or {}).get("stop_bps")
        stop_px = (float(t["entry_px"]) * (1 - float(sb) / 1e4)) if sb else None
        plan = build(t["symbol"], t["strategy"], float(t["entry_px"]),
                     target_px, stop_px, atr, why=why)
        record(plan, mode, float(t["ts_open"]))
        out = _replay_outcome(t["symbol"], float(t["ts_open"]), float(t["ts_close"]),
                              float(t["entry_px"]), target_px, plan["window_h"])
        if out:
            on_time = out["hit_h"] is not None and out["hit_h"] <= plan["window_h"]
            if on_time:
                state, note = "on_track", (
                    f"reached the predicted price at {out['hit_h']:.1f}h, inside the "
                    f"{plan['window_h']:g}h window")
            elif out["hit_h"] is not None:
                state, note = "failed", (
                    f"reached it, but at {out['hit_h']:.1f}h against a "
                    f"{plan['window_h']:g}h window — the price was right, the timing was not")
            elif out["final_pct"] >= 0:
                state, note = "exit_seeking", (
                    f"never reached the predicted price in {out['lived_h']:.1f}h, "
                    f"but closed {out['final_pct']:+.2f}%")
            else:
                state, note = "failed", (
                    f"never reached the predicted price; closed {out['final_pct']:+.2f}% "
                    f"after {out['lived_h']:.1f}h")
            mark(t["symbol"], t["strategy"], mode, float(t["ts_open"]), state, note,
                 out["worst_pct"], out["best_pct"])
            verdicts[state] = verdicts.get(state, 0) + 1
        made += 1
    return {"built": made, "already_had_one": skipped, "verdicts": verdicts}


def accuracy(mode: str = "paper") -> list[dict]:
    """How often each strategy's prediction was right. The scorecard the
    operator asked for: 'how it was evaluated and measured for accuracy'."""
    ensure_schema()
    by: dict[str, dict] = {}
    for r in db.query("SELECT strategy, state, p_reach, ratio, window_h, target_frac "
                      "FROM trade_plans WHERE mode=?", (mode,)):
        d = by.setdefault(r["strategy"], {
            "strategy": r["strategy"], "n": 0, "on_track": 0, "failed": 0,
            "exit_seeking": 0, "wobbling": 0, "p_reach_sum": 0.0, "ratio_sum": 0.0})
        d["n"] += 1
        d[r["state"]] = d.get(r["state"], 0) + 1
        d["p_reach_sum"] += float(r["p_reach"] or 0.0)
        d["ratio_sum"] += float(r["ratio"] or 0.0)
    out = []
    for d in by.values():
        n = max(1, d["n"])
        d["hit_rate"] = d["on_track"] / n * 100.0
        d["predicted_rate"] = d.pop("p_reach_sum") / n * 100.0
        d["avg_ratio"] = d.pop("ratio_sum") / n
        # the number that matters: did the model know how good it was?
        d["calibration_gap"] = d["hit_rate"] - d["predicted_rate"]
        out.append(d)
    return sorted(out, key=lambda x: -x["n"])
