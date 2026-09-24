"""The lab: score every strategy every day, and act on the score.

2026-09-23, the operator, after a -$50.46 day: "you should have a lab to
specifically try different things and collect data daily and learn then promote
or de-promote strategies or models."

WHAT IT DOES, once a day:

  1. Scores each strategy on its closed trades -- return per trade as a
     percentage of what was risked, not dollars, so a strategy is not rewarded
     for having been given bigger positions.
  2. Writes the scorecard to `lab_scores`, one row per strategy per day, so the
     history is a record and not a recomputation.
  3. Demotes a strategy whose evidence is clearly bad, and promotes one whose
     evidence has recovered -- through `risk.control`, which the operator's own
     switch outranks. The lab can never re-enable something he turned off.

WHY THE BAR IS A t-STATISTIC AND NOT A P&L:
  A strategy down $17 on 12 trades and one down $17 on 200 trades are not the
  same evidence. The t is mean / standard error: how sure we are the mean is
  really below zero rather than noise. MIN_TRADES stops it acting on three
  trades; DEMOTE_T is how sure it has to be.

WHY THE BOOK-LEVEL NUMBER IS HERE TOO:
  2026-09-23 the desk lost $49.12 in 98 minutes as five positions stopped out
  together. The held coins correlate +0.43 and the book is worth about 2
  independent bets, not 11. A per-strategy scorecard cannot see that, so the
  day's worst hour is recorded beside it -- what actually hurt, in one number.
"""
from __future__ import annotations

import math
import statistics as st
import time

from app.core import clock, db
from app.risk import control

MIN_TRADES = 15          # below this, the lab records but does not act
DEMOTE_T = -1.5          # how sure we must be that the mean is below zero
PROMOTE_T = 0.0          # back on once the evidence is no longer negative
WINDOW_TRADES = 60       # rolling: a strategy is judged on its recent record
SOURCE = "lab"


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS lab_scores (
        id INTEGER PRIMARY KEY,
        day TEXT NOT NULL, strategy TEXT NOT NULL, mode TEXT NOT NULL,
        n INTEGER, mean_pct REAL, sd_pct REAL, t_stat REAL, win_rate REAL,
        net_usd REAL, verdict TEXT, acted INTEGER DEFAULT 0, note TEXT,
        computed_ts REAL)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS lab_scores_key "
               "ON lab_scores(day, strategy, mode)")
    db.execute("""CREATE TABLE IF NOT EXISTS lab_book (
        day TEXT PRIMARY KEY, mode TEXT, net_usd REAL, gross_usd REAL,
        cost_usd REAL, trades INTEGER, worst_hour TEXT, worst_hour_usd REAL,
        stops_in_worst_hour INTEGER, computed_ts REAL)""")


def score(strategy: str, mode: str = "paper") -> dict:
    """One strategy's recent record. Percentages of what was risked."""
    rows = db.query(
        "SELECT net_pnl_usd n, qty, entry_px FROM trades WHERE mode=? AND strategy=? "
        "AND ts_close IS NOT NULL AND entry_px > 0 ORDER BY ts_close DESC LIMIT ?",
        (mode, strategy, WINDOW_TRADES))
    v = [float(r["n"]) / abs(float(r["qty"]) * float(r["entry_px"])) * 100.0
         for r in rows if r["qty"] and r["entry_px"]]
    if not v:
        return {"strategy": strategy, "n": 0, "mean_pct": 0.0, "sd_pct": 0.0,
                "t_stat": 0.0, "win_rate": 0.0, "net_usd": 0.0,
                "verdict": "no trades yet"}
    mean = st.mean(v)
    sd = st.pstdev(v) if len(v) > 1 else 0.0
    # ZERO VARIANCE IS MAXIMUM CERTAINTY, NOT ZERO CERTAINTY. A strategy that
    # loses the same amount on every single trade is the surest loser there is,
    # and `mean / (0 / sqrt(n))` is a divide-by-zero that the obvious guard
    # (`if sd else 0.0`) turns into t = 0 -- the score of a coin flip. It would
    # never have been switched off. The floor is one basis point of return:
    # below that the sample is effectively noiseless and the sign is the answer.
    se = max(sd / math.sqrt(len(v)), 0.01 / math.sqrt(len(v))) if len(v) > 1 else 0.0
    t = (mean / se) if se else 0.0
    return {"strategy": strategy, "n": len(v), "mean_pct": mean, "sd_pct": sd,
            "t_stat": t, "win_rate": sum(1 for x in v if x > 0) / len(v) * 100.0,
            "net_usd": sum(float(r["n"]) for r in rows),
            "verdict": _verdict(len(v), t)}


def _verdict(n: int, t: float) -> str:
    if n < MIN_TRADES:
        return f"not enough trades yet ({n}/{MIN_TRADES})"
    if t <= DEMOTE_T:
        return "losing, and clearly enough to act"
    if t >= PROMOTE_T:
        return "not losing"
    return "losing, but not clearly enough to act"


def book_day(day: str | None = None, mode: str = "paper") -> dict:
    """What the day did, and the hour that did it.

    The hour matters because the failure mode is correlated: on 2026-09-23 five
    stops fired inside 98 minutes. A daily total hides that; the worst hour
    names it.
    """
    day = day or clock.day_key(time.time())
    t0, t1 = clock.bounds_for_day(day)
    rows = db.query("SELECT ts_close, net_pnl_usd n, gross_pnl_usd g, cost_usd k "
                    "FROM trades WHERE mode=? AND ts_close >= ? AND ts_close < ?",
                    (mode, t0, t1))
    if not rows:
        return {"day": day, "trades": 0, "net_usd": 0.0}
    by_hour: dict[str, list] = {}
    for r in rows:
        h = clock.hour_key(float(r["ts_close"])) if hasattr(clock, "hour_key") else \
            str(int((float(r["ts_close"]) - t0) // 3600))
        by_hour.setdefault(h, []).append(float(r["n"]))
    worst = min(by_hour.items(), key=lambda kv: sum(kv[1]))
    return {"day": day, "mode": mode, "trades": len(rows),
            "net_usd": sum(float(r["n"]) for r in rows),
            "gross_usd": sum(float(r["g"] or 0) for r in rows),
            "cost_usd": sum(float(r["k"] or 0) for r in rows),
            "worst_hour": f"hour {worst[0]} of the day",
            "worst_hour_usd": sum(worst[1]),
            "stops_in_worst_hour": len(worst[1])}


def run(mode: str = "paper", act: bool = True) -> dict:
    """Score everything, write it down, and act where the evidence is clear."""
    ensure_schema()
    from app.strategy.registry import studied
    day = clock.day_key(time.time())
    out = {"day": day, "scored": [], "demoted": [], "promoted": [], "book": {}}

    # UNION, not the roster. A strategy with real trades must be scored whether
    # or not it is on the active list -- scoring only the roster is how a
    # retired-but-still-holding strategy becomes invisible (checklist 5.39, the
    # same shape that stopped burst_catch being studied).
    names = list(studied(mode))
    try:
        for r in db.query("SELECT DISTINCT strategy FROM trades WHERE mode=? "
                          "AND ts_close IS NOT NULL", (mode,)):
            if r["strategy"] and r["strategy"] not in names:
                names.append(r["strategy"])
    except Exception:
        pass

    for name in names:
        s = score(name, mode)
        acted = 0
        note = ""
        if act and s["n"] >= MIN_TRADES:
            state = control.state(name)
            on = state.get("enabled", True)
            if s["t_stat"] <= DEMOTE_T and on:
                r = control.set_state(
                    name, enabled=False, source=SOURCE,
                    note=(f"lab {day}: {s['n']} trades, {s['mean_pct']:+.2f}%/trade, "
                          f"t={s['t_stat']:+.2f}. Losing clearly enough to stop."))
                if r:
                    acted = 1; note = "demoted"; out["demoted"].append(name)
            elif s["t_stat"] >= PROMOTE_T and not on:
                r = control.set_state(
                    name, enabled=True, source=SOURCE,
                    note=(f"lab {day}: {s['n']} trades, {s['mean_pct']:+.2f}%/trade, "
                          f"t={s['t_stat']:+.2f}. No longer losing."))
                if r:
                    acted = 1; note = "promoted"; out["promoted"].append(name)
        db.execute(
            """INSERT INTO lab_scores(day,strategy,mode,n,mean_pct,sd_pct,t_stat,
                                      win_rate,net_usd,verdict,acted,note,computed_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(day,strategy,mode) DO UPDATE SET
                 n=excluded.n, mean_pct=excluded.mean_pct, sd_pct=excluded.sd_pct,
                 t_stat=excluded.t_stat, win_rate=excluded.win_rate,
                 net_usd=excluded.net_usd, verdict=excluded.verdict,
                 acted=excluded.acted, note=excluded.note,
                 computed_ts=excluded.computed_ts""",
            (day, name, mode, s["n"], s["mean_pct"], s["sd_pct"], s["t_stat"],
             s["win_rate"], s["net_usd"], s["verdict"], acted, note, time.time()))
        out["scored"].append(s)

    b = book_day(day, mode)
    if b.get("trades"):
        db.execute("""INSERT INTO lab_book(day,mode,net_usd,gross_usd,cost_usd,trades,
                        worst_hour,worst_hour_usd,stops_in_worst_hour,computed_ts)
                      VALUES (?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(day) DO UPDATE SET net_usd=excluded.net_usd,
                        gross_usd=excluded.gross_usd, cost_usd=excluded.cost_usd,
                        trades=excluded.trades, worst_hour=excluded.worst_hour,
                        worst_hour_usd=excluded.worst_hour_usd,
                        stops_in_worst_hour=excluded.stops_in_worst_hour,
                        computed_ts=excluded.computed_ts""",
                   (day, mode, b["net_usd"], b.get("gross_usd", 0), b.get("cost_usd", 0),
                    b["trades"], b.get("worst_hour"), b.get("worst_hour_usd", 0),
                    b.get("stops_in_worst_hour", 0), time.time()))
    out["book"] = b
    return out


def history(mode: str = "paper", days: int = 30) -> dict:
    ensure_schema()
    return {
        "scores": [dict(r) for r in db.query(
            "SELECT * FROM lab_scores WHERE mode=? ORDER BY day DESC, t_stat ASC LIMIT ?",
            (mode, days * 12))],
        "book": [dict(r) for r in db.query(
            "SELECT * FROM lab_book WHERE mode=? ORDER BY day DESC LIMIT ?", (mode, days))],
    }
