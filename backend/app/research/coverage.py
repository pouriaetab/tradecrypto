"""What moved today, what we could see, and what we had no eyes for.

The question this answers is the operator's, asked on 2026-09-13 after a day when
ten Robinhood-tradable coins had a move that cleared the round trip and this desk
fired zero signals: "did you add this as something we see and act on now?"

A strategy that cannot see a shape is not a bug that shows up in a log. It shows
up as silence. So the silence is measured here and put on screen: every tradable
coin's best move from the open, whether that move was big enough to be worth
taking at all, and which of the active strategies produced a signal for it.

A coin in `uncovered` moved enough to pay for itself and NOTHING looked at it.
That list is the specification for the next strategy -- it is how day_climb came
to exist.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import db
from app.execution import rh_spread

TZ = ZoneInfo("America/Chicago")


def _round_trip_pct(symbol: str) -> float:
    try:
        return float(rh_spread.get(symbol)["round_trip_bps"]) / 100.0
    except Exception:
        return 1.9182


def today(day: str | None = None) -> dict:
    d = datetime.strptime(day, "%Y-%m-%d").date() if day else datetime.now(TZ).date()
    t0 = datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()
    t1 = t0 + 86400

    rows = db.query(
        """SELECT b.symbol, MIN(b.ts) t_first, MAX(b.ts) t_last,
                  MAX(b.high) hi, MIN(b.low) lo, COUNT(*) n
           FROM bars b JOIN universe u ON u.symbol = b.symbol
           WHERE b.granularity = 900 AND b.ts >= ? AND b.ts < ?
             AND u.active = 1 AND u.rh_confirmed = 1 AND b.close IS NOT NULL
           GROUP BY b.symbol""", (t0, t1))
    if not rows:
        return {"day": str(d), "coins": 0, "note": "no bars for this day yet"}

    # Which coins each active strategy actually fired on today.
    seen: dict[str, set[str]] = {}
    for r in db.query(
            "SELECT DISTINCT symbol, strategy FROM signals WHERE ts >= ? AND ts < ?", (t0, t1)):
        seen.setdefault(r["symbol"], set()).add(r["strategy"])
    traded = {r["symbol"] for r in db.query(
        "SELECT DISTINCT symbol FROM orders WHERE ts_decided >= ? AND ts_decided < ? "
        # A rejected or pending order is NOT a trade. Without this filter a coin
        # the desk saw, tried to trade and failed to trade was reported as traded,
        # which quietly removed it from the "missed" list -- exactly the rows most
        # worth looking at.
        "AND status='filled'", (t0, t1))}

    out = []
    for r in rows:
        if (r["n"] or 0) < 4:
            continue
        first = db.query_one("SELECT close FROM bars WHERE symbol=? AND granularity=900 AND ts=?",
                             (r["symbol"], r["t_first"]))
        last = db.query_one("SELECT close FROM bars WHERE symbol=? AND granularity=900 AND ts=?",
                            (r["symbol"], r["t_last"]))
        if not (first and last and first["close"] and last["close"]):
            continue
        op, cl, hi, lo_ = float(first["close"]), float(last["close"]), r["hi"], r["lo"]
        if not (op and hi and lo_):
            continue
        rt = _round_trip_pct(r["symbol"])
        best = (hi / op - 1.0) * 100.0
        strategies = sorted(seen.get(r["symbol"], set()))
        out.append({
            "symbol": r["symbol"],
            "change_pct": (cl / op - 1.0) * 100.0,
            "best_from_open_pct": best,
            "range_pct": (hi / lo_ - 1.0) * 100.0,
            "round_trip_pct": rt,
            "worth_taking": best >= rt,          # the move paid for itself
            "seen_by": strategies,
            "traded": r["symbol"] in traded,
        })

    out.sort(key=lambda x: -x["best_from_open_pct"])
    worth = [x for x in out if x["worth_taking"]]
    uncovered = [x for x in worth if not x["seen_by"]]
    missed = [x for x in worth if x["seen_by"] and not x["traded"]]
    return {
        "day": str(d),
        "as_of": time.time(),
        "coins": len(out),
        "worth_taking": len(worth),
        "uncovered_count": len(uncovered),
        "missed_count": len(missed),
        "traded_count": sum(1 for x in out if x["traded"]),
        # The headline: moves that paid for themselves and no strategy even looked.
        "uncovered": uncovered[:12],
        # Saw it, did not act (usually a full book or a risk block).
        "missed": missed[:12],
        "top": out[:15],
        "verdict": (
            f"{len(worth)} tradable coin(s) moved enough to cover their own spread. "
            + (f"{len(uncovered)} of them were invisible to every active strategy."
               if uncovered else "Every one of them was visible to at least one strategy.")
        ),
    }
