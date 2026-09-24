"""Trade budget: "take 2 trades over the next 24 hours" — and hold out for good ones.

The naive version of a trade quota takes the first N signals that clear the
hurdle. That is almost always wrong: the first signal of the day is rarely the
best one, and spending a slot early means being flat when something better shows
up at 2pm.

The right frame is a known problem — sequential assignment with a finite number
of slots and a deadline (Albright 1974; the "house-selling" family of optimal
stopping problems). With `s` slots left and `k` time steps remaining, there is a
reservation threshold, and the rule is simply:

    take this signal  <=>  its quality exceeds the threshold

The threshold comes from a dynamic program over the empirical distribution of
signal quality:

    V(k, s) = (1-p)·V(k-1, s) + p·E[ max( q + V(k-1, s-1),  V(k-1, s) ) ]
    theta(k, s) = V(k-1, s) − V(k-1, s−1)

where p is the probability a signal arrives in one time step. The threshold has
exactly the properties you would want and did not have to hand-tune:

  * it FALLS as the deadline approaches — a slot you never spend is worth nothing
  * it RISES when you have few slots relative to time — be picky early
  * it adapts to the day: on a quiet day the quality distribution shifts down and
    the bar comes down with it, rather than the system sitting on its hands

Quality is measured as expected net edge in basis points AFTER the per-coin cost
hurdle, so "good" always means "good after what Robinhood charges for that coin".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

import numpy as np

from app.config import get_settings
from app.core import clock, db

DEFAULT_TZ_OFFSET_H = -6.0        # America/Chicago; the operator's trading day
TIME_STEPS = 48                   # DP resolution over the window
MIN_QUALITY_SAMPLES = 30

# A budget is a CAP, never a target.
#
# The dynamic program's threshold decays toward zero as the deadline approaches,
# because in the classic formulation an unspent slot is worthless. Here it is
# not: an unspent slot means capital preserved and no spread paid, which is
# strictly better than a marginal trade. Quality is already measured NET of the
# cost hurdle, so a floor of 0 means "must at least clear what Robinhood charges";
# raising TC_BUDGET_MIN_QUALITY_BPS demands a margin on top of that.
#
# Consequence, stated plainly: ask for 10 trades and get 3, and the day worked
# correctly. Slots expiring unused is a good outcome, not a missed one.
DEFAULT_MIN_QUALITY_BPS = 0.0


def _local_day_bounds(tz_offset_h: float = DEFAULT_TZ_OFFSET_H) -> tuple[float, float]:
    """Midnight to 23:59:59 in the operator's local time, as unix seconds.

    He defines a trading day as 12:00am to 11:59pm local, so the ledger, the
    budget and the daily P&L all cut on that boundary rather than UTC.
    """
    # A fixed -6.0 offset is wrong for the eight months Chicago spends on
    # daylight time. Use the real zone.
    start = clock.local_day_start()
    return start, start + 86400 - 1


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS trade_budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts REAL NOT NULL,
        window_start REAL NOT NULL,
        window_end REAL NOT NULL,
        slots_total INTEGER NOT NULL,
        slots_used INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL,
        strategies TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        note TEXT,
        decisions_json TEXT
    )""")


@dataclass
class BudgetDecision:
    take: bool
    quality_bps: float
    threshold_bps: float
    slots_left: int
    time_left_s: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


# ── the quality distribution ─────────────────────────────────────────────────
def recent_quality_samples(days: float = 14.0, strategies: list[str] | None = None) -> np.ndarray:
    """Net expected edge (edge − hurdle) of recent signals, in bps.

    Rejected signals are included on purpose: they are part of the distribution
    of what a day offers, and excluding them would make the system think every
    opportunity is a good one.
    """
    since = time.time() - days * 86400
    sql = ("SELECT expected_edge_bps, cost_hurdle_bps FROM signals "
           "WHERE ts >= ? AND expected_edge_bps IS NOT NULL")
    params: list = [since]
    if strategies:
        sql += " AND strategy IN (%s)" % ",".join("?" * len(strategies))
        params.extend(strategies)
    rows = db.query(sql, params)
    q = np.array([(r["expected_edge_bps"] or 0) - (r["cost_hurdle_bps"] or 0) for r in rows],
                 dtype=float)
    return q[np.isfinite(q)]


def arrival_rate_per_step(window_s: float, days: float = 14.0,
                          strategies: list[str] | None = None) -> float:
    """Probability that a signal shows up in one DP time step."""
    since = time.time() - days * 86400
    sql = "SELECT COUNT(*) c FROM signals WHERE ts >= ?"
    params: list = [since]
    if strategies:
        sql += " AND strategy IN (%s)" % ",".join("?" * len(strategies))
        params.extend(strategies)
    n = db.query_one(sql, params)["c"] or 0
    elapsed = max(time.time() - since, 1.0)
    per_second = n / elapsed
    step_s = window_s / TIME_STEPS
    return float(min(0.95, per_second * step_s))


# ── the dynamic program ──────────────────────────────────────────────────────
def thresholds(quality: np.ndarray, p_arrival: float, slots: int,
               steps: int = TIME_STEPS) -> np.ndarray:
    """theta[k, s]: the bar a signal must clear with k steps and s slots left.

    Solved by backward induction over the empirical quality distribution, so no
    parametric assumption about how good opportunities are is needed.
    """
    q = np.sort(quality)
    if q.size == 0:
        return np.zeros((steps + 1, slots + 1))

    V = np.zeros((steps + 1, slots + 1))
    theta = np.zeros((steps + 1, slots + 1))
    for k in range(1, steps + 1):
        for s in range(1, slots + 1):
            th = V[k - 1, s] - V[k - 1, s - 1]      # value of keeping the slot
            theta[k, s] = th
            gain = np.maximum(q - th, 0.0).mean()   # E[(q - theta)+]
            V[k, s] = V[k - 1, s] + p_arrival * gain
    return theta


# ── the public interface ─────────────────────────────────────────────────────
def create(slots: int, window_hours: float = 24.0, mode: str = "paper",
           strategies: list[str] | None = None, note: str = "") -> dict:
    ensure_schema()
    now = time.time()
    if window_hours >= 24 and abs(window_hours - 24) < 0.01:
        start, end = _local_day_bounds()
        end = max(end, now + 60)
    else:
        start, end = now, now + window_hours * 3600
    bid = db.execute(
        """INSERT INTO trade_budgets(created_ts, window_start, window_end, slots_total,
                                     mode, strategies, note)
           VALUES (?,?,?,?,?,?,?)""",
        (now, start, end, int(slots), mode,
         json.dumps(strategies) if strategies else None, note))
    db.log_event("INFO", "budget",
                 f"budget #{bid}: {slots} trade(s) between "
                 f"{datetime.fromtimestamp(start).isoformat(timespec='minutes')} and "
                 f"{datetime.fromtimestamp(end).isoformat(timespec='minutes')}")
    return get(bid)


def active(mode: str | None = None) -> list[dict]:
    ensure_schema()
    now = time.time()
    sql = ("SELECT * FROM trade_budgets WHERE status='active' AND window_end > ? "
           "AND slots_used < slots_total")
    params: list = [now]
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    return [_decorate(r) for r in db.query(sql + " ORDER BY created_ts DESC", params)]


def get(budget_id: int) -> dict:
    ensure_schema()
    r = db.query_one("SELECT * FROM trade_budgets WHERE id=?", (budget_id,))
    return _decorate(r) if r else {}


def cancel(budget_id: int) -> dict:
    db.execute("UPDATE trade_budgets SET status='cancelled' WHERE id=?", (budget_id,))
    return get(budget_id)


def _decorate(r: dict) -> dict:
    now = time.time()
    slots_left = max(0, r["slots_total"] - r["slots_used"])
    time_left = max(0.0, r["window_end"] - now)
    total = max(r["window_end"] - r["window_start"], 1.0)
    strategies = json.loads(r["strategies"]) if r["strategies"] else None

    q = recent_quality_samples(strategies=strategies)
    p = arrival_rate_per_step(total, strategies=strategies)
    floor = float(getattr(get_settings(), "budget_min_quality_bps", DEFAULT_MIN_QUALITY_BPS))
    th = None
    dp_th = None
    if q.size >= MIN_QUALITY_SAMPLES and slots_left > 0:
        tbl = thresholds(q, p, slots_left)
        k = int(np.clip(round(TIME_STEPS * time_left / total), 1, TIME_STEPS))
        dp_th = float(tbl[k, slots_left])
        # The floor is what stops the deadline from forcing a bad trade.
        th = max(dp_th, floor)

    return {
        **r,
        "strategies": strategies,
        "slots_left": slots_left,
        "time_left_s": time_left,
        "time_left_hours": time_left / 3600,
        "pct_window_elapsed": 100 * (1 - time_left / total),
        "current_threshold_bps": th,
        "quality_samples": int(q.size),
        "quality_median_bps": float(np.median(q)) if q.size else None,
        "quality_p90_bps": float(np.quantile(q, 0.9)) if q.size else None,
        "arrival_prob_per_step": p,
        "dp_threshold_bps": dp_th,
        "quality_floor_bps": floor,
        "floor_is_binding": bool(dp_th is not None and floor > dp_th),
        "is_a_cap_not_a_target": True,
        "threshold_status": (
            "learning" if q.size < MIN_QUALITY_SAMPLES else
            "active" if th is not None else "exhausted"),
        "explanation": (
            "With slots left and time remaining, the system holds out for a signal "
            "better than the threshold. The threshold falls as the deadline nears, "
            "but never below the quality floor — so an approaching deadline can make "
            "the system less picky, and can never make it take a trade that fails to "
            "clear the cost hurdle. This is a CAP on how many trades may happen, not "
            "a quota to fill: asking for 10 and getting 3 means the day only offered 3."
        ),
    }


def evaluate(quality_bps: float, mode: str = "paper",
             strategy: str | None = None, experiment: bool = False) -> BudgetDecision:
    """Should this signal spend a slot? Called by the engine before every order.

    `experiment` is the PAPER-ONLY branch that deliberately takes sub-hurdle
    signals to measure them. It waives the quality bar and nothing else -- slot
    counts, strategy reservations and the window still apply. The engine used to
    achieve this by passing max(quality, 0.01), which forged a positive number
    into the floor and made the guarantee in this module untrue. Saying it out
    loud keeps the real quality in the record.
    """
    buds = active(mode)
    if not buds:
        return BudgetDecision(True, quality_bps, float("-inf"), 0, 0.0,
                              "no trade budget set — the normal risk limits apply")
    b = buds[0]
    if b["strategies"] and strategy and strategy not in b["strategies"]:
        return BudgetDecision(False, quality_bps, float("inf"), b["slots_left"],
                              b["time_left_s"],
                              f"budget #{b['id']} is reserved for {b['strategies']}")
    if b["slots_left"] <= 0:
        return BudgetDecision(False, quality_bps, float("inf"), 0, b["time_left_s"],
                              f"budget #{b['id']} is spent")
    th = b["current_threshold_bps"]
    if th is None:
        # Not enough history to compute a threshold. The budget stops filtering,
        # but the floor still applies: a signal that does not clear the cost
        # hurdle is never taken, budget or no budget.
        floor = b.get("quality_floor_bps", DEFAULT_MIN_QUALITY_BPS)
        if quality_bps < floor and experiment:
            return BudgetDecision(
                True, quality_bps, floor, b["slots_left"], b["time_left_s"],
                f"EXPERIMENT (paper only): quality {quality_bps:.0f} bps is below the "
                f"{floor:.0f} bps floor and is taken anyway, to measure it")
        if quality_bps < floor:
            return BudgetDecision(
                False, quality_bps, floor, b["slots_left"], b["time_left_s"],
                f"quality {quality_bps:.0f} bps is below the {floor:.0f} bps floor — "
                f"the signal does not clear its own cost hurdle")
        return BudgetDecision(
            True, quality_bps, floor, b["slots_left"], b["time_left_s"],
            f"only {b['quality_samples']} historical signals — too few to set a "
            f"threshold, so the budget is not filtering beyond the cost floor")
    take = quality_bps >= th
    if not take and experiment:
        return BudgetDecision(
            True, quality_bps, th, b["slots_left"], b["time_left_s"],
            f"EXPERIMENT (paper only): quality {quality_bps:.0f} bps is below the "
            f"{th:.0f} bps bar and is taken anyway, to measure it")
    return BudgetDecision(
        take, quality_bps, th, b["slots_left"], b["time_left_s"],
        (f"quality {quality_bps:.0f} bps clears the {th:.0f} bps bar with "
         f"{b['slots_left']} slot(s) and {b['time_left_hours']:.1f}h left")
        if take else
        (f"quality {quality_bps:.0f} bps is below the {th:.0f} bps bar — holding out; "
         f"{b['slots_left']} slot(s) and {b['time_left_hours']:.1f}h left. Slots are "
         f"allowed to expire unused."))


def consume(budget_id: int, symbol: str, quality_bps: float) -> None:
    ensure_schema()
    r = db.query_one("SELECT decisions_json, slots_used FROM trade_budgets WHERE id=?", (budget_id,))
    if not r:
        return
    log = json.loads(r["decisions_json"]) if r["decisions_json"] else []
    log.append({"ts": time.time(), "symbol": symbol, "quality_bps": quality_bps})
    db.execute("UPDATE trade_budgets SET slots_used=slots_used+1, decisions_json=? WHERE id=?",
               (json.dumps(log[-100:]), budget_id))


def spend_if_allowed(quality_bps: float, symbol: str, mode: str = "paper",
                     strategy: str | None = None, experiment: bool = False) -> BudgetDecision:
    d = evaluate(quality_bps, mode, strategy, experiment=experiment)
    if d.take:
        buds = active(mode)
        if buds:
            consume(buds[0]["id"], symbol, quality_bps)
    return d
