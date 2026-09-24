"""One report per day, built automatically, kept forever.

What it answers, every day, without anyone pressing anything:

  * what moved enough to be worth taking
  * what we actually traded, and what that cost
  * what we MISSED, split into "a strategy saw it and something stopped us" and
    "nothing was even looking" -- the second list is the specification for the
    next strategy
  * which strategies fired, and why their signals were refused
  * what the day taught: hit rate, average move captured versus available

Reports are stored as rows, not recomputed on demand, so the history is stable
and a range of days can be aggregated and charted. A day is rebuilt only if it
is today (still moving) or if `force` is passed.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import db
from app.core import mode as mode_mod
from app.execution import rh_spread

TZ = ZoneInfo("America/Chicago")

# Bump this whenever the MEANING of a stored report changes. A stored row is
# rebuilt when something happened on that day after it was built -- which is a
# question about the DATA and says nothing about the CODE. So when the logic
# changed on 2026-09-22 (the day's P&L had been summing every mode, and was
# counting 21 retired burst_catch trades into the paper book), every row already
# on disk kept its wrong number: nothing new had happened on those days, so
# nothing triggered a rebuild. The fix shipped and the operator still saw
# -$42.65 instead of -$18.41.
#
#   1  the original
#   2  2026-09-22: the day's P&L and _last_activity are scoped to the running
#      mode, so retired (mode='lab') trades are no longer in the book's day
REPORT_VERSION = 2


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS daily_reports (
        day TEXT PRIMARY KEY,
        built_at REAL NOT NULL,
        summary_json TEXT NOT NULL
    )""")


def _rt_pct(symbol: str) -> float:
    try:
        return float(rh_spread.get(symbol)["round_trip_bps"]) / 100.0
    except Exception:
        return 1.9182


def _bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    # NOT t0 + 86400. A Chicago day is 23 or 25 hours twice a year, and a fixed
    # 86,400 either drops the last hour (Nov 1) or runs an hour into the next day
    # (Mar 8), double-counting those trades in two reports.
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


# Shapes we have MEASURED, so the "nothing was watching" list stops nagging about
# moves that were never catchable.
#
# Across 14 days, 270 coin-days cleared 2x their own spread. Classified by what
# actually happened during the day: 44% were day_climb's shape (a 5-15% climb
# over four hours), 6% were a deep dip, 0% were an 18%-in-4-hours pump, and
# **50% were a slow drift** -- up a few percent across a whole day with no
# four-hour move at all.
#
# The slow drift was then measured properly: best train variant (up 4-6% over 12
# hours, 12h hold) gave held-out gross -0.17% over 7,003 trades, t = -50.70. It
# is negative even at the $50K exchange tier's 0.50% round trip (-0.67% a trade).
#
# So half of what looks like a coverage gap is hindsight, not money. A move that
# ended +4% is only an opportunity if something could have known at entry, and
# for this shape nothing can. The report labels it rather than listing it as a
# debt.
UNCATCHABLE = {
    "slow_drift": ("measured: held-out gross -0.17% over 7,003 trades, t=-50.7 — "
                   "no forward edge, negative even at the cheapest fee tier"),
}


def _shape_of(sym: str, t0: float, t1: float) -> str:
    """What shape was this day's move, in terms a strategy could have seen?"""
    rows = db.query(
        """SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=3600
           AND ts >= ? AND ts < ? ORDER BY ts""", (sym, t0 - 30 * 3600, t1))
    if len(rows) < 30:
        return "unknown"
    hi = [r["high"] for r in rows]
    cl = [r["close"] for r in rows]
    idx = [k for k, r in enumerate(rows) if t0 <= r["ts"] < t1]
    for k in idx:
        if k < 28 or not cl[k] or not cl[k - 4]:
            continue
        c4 = (cl[k] / cl[k - 4] - 1) * 100
        pk = max((x for x in hi[k - 23:k + 1] if x), default=None)
        depth = (cl[k] / pk - 1) * 100 if pk else 0
        if c4 >= 18:
            return "pump"
        if 5 <= c4 <= 15:
            return "climb"
        if depth <= -10 and cl[k - 1] and cl[k] > cl[k - 1]:
            return "deep_dip"
    return "slow_drift"


TODAY_REBUILD_S = 120.0


def build(day: str) -> dict:
    """Compute one day's report from stored bars, signals, orders and trades."""
    t0, t1 = _bounds(day)

    # ── what every tradable coin did ──────────────────────────────────────────
    agg = db.query(
        """SELECT b.symbol, MIN(b.ts) t_first, MAX(b.ts) t_last,
                  MAX(b.high) hi, MIN(b.low) lo, SUM(COALESCE(b.volume,0)) vol,
                  COUNT(*) n
           FROM bars b JOIN universe u ON u.symbol = b.symbol
           WHERE b.granularity = 900 AND b.ts >= ? AND b.ts < ?
             AND u.active = 1 AND u.rh_confirmed = 1 AND b.close IS NOT NULL
           GROUP BY b.symbol""", (t0, t1))

    seen: dict[str, set[str]] = {}
    for r in db.query("SELECT DISTINCT symbol, strategy FROM signals "
                      "WHERE ts >= ? AND ts < ?", (t0, t1)):
        seen.setdefault(r["symbol"], set()).add(r["strategy"])
    traded = {r["symbol"] for r in db.query(
        "SELECT DISTINCT symbol FROM orders WHERE ts_decided >= ? AND ts_decided < ? "
        # A rejected or pending order is NOT a trade. Without this filter a coin
        # the desk saw, tried to trade and failed to trade was reported as traded,
        # which quietly removed it from the "missed" list -- exactly the rows most
        # worth looking at.
        "AND status='filled'", (t0, t1))}

    coins = []
    for r in agg:
        if (r["n"] or 0) < 4:
            continue
        f = db.query_one("SELECT close FROM bars WHERE symbol=? AND granularity=900 AND ts=?",
                         (r["symbol"], r["t_first"]))
        l = db.query_one("SELECT close FROM bars WHERE symbol=? AND granularity=900 AND ts=?",
                         (r["symbol"], r["t_last"]))
        if not (f and l and f["close"] and l["close"]):
            continue
        op, cl, hi, lo_ = float(f["close"]), float(l["close"]), r["hi"], r["lo"]
        if not (op and hi and lo_):
            continue
        rt = _rt_pct(r["symbol"])
        best = (hi / op - 1.0) * 100.0
        strategies = sorted(seen.get(r["symbol"], set()))
        coins.append({
            "symbol": r["symbol"],
            "open": op, "close": cl, "high": hi, "low": lo_,
            "change_pct": (cl / op - 1.0) * 100.0,
            "best_from_open_pct": best,
            "drawdown_pct": (lo_ / op - 1.0) * 100.0,
            "range_pct": (hi / lo_ - 1.0) * 100.0,
            "round_trip_pct": rt,
            # "worth taking" means the best move of the day beat the round trip.
            # That is a PERFECT-TIMING bar -- it assumes selling at the exact high
            # -- so on its own it over-counts badly. A coin whose best move was
            # +2.3% against a 1.92% toll leaves 0.38%, and only if you nail the
            # top tick. So opportunities are graded, and the grade is what the
            # coverage number should be read against.
            "worth_taking": best >= rt,
            "margin_x": round(best / rt, 2) if rt else None,
            "grade": ("strong" if best >= 3 * rt else
                      "workable" if best >= 2 * rt else
                      "marginal" if best >= rt else "no"),
            "seen_by": strategies,
            "traded": r["symbol"] in traded,
        })
    for c in coins:
        if c["worth_taking"] and not c["seen_by"]:
            c["shape"] = _shape_of(c["symbol"], t0, t1)
            c["catchable"] = c["shape"] not in UNCATCHABLE
            if not c["catchable"]:
                c["why_not"] = UNCATCHABLE[c["shape"]]
    coins.sort(key=lambda x: -x["best_from_open_pct"])
    worth = [c for c in coins if c["worth_taking"]]
    real = [c for c in coins if c["grade"] in ("strong", "workable")]
    uncovered = [c for c in worth if not c["seen_by"]]
    uncovered_real = [c for c in real if not c["seen_by"] and c.get("catchable", True)]
    uncatchable = [c for c in real if not c["seen_by"] and not c.get("catchable", True)]
    missed = [c for c in worth if c["seen_by"] and not c["traded"]]

    # ── what the strategies did ───────────────────────────────────────────────
    per_strategy = []
    for r in db.query(
            """SELECT strategy, COUNT(*) n,
                      SUM(CASE WHEN decision LIKE 'taken%' THEN 1 ELSE 0 END) taken,
                      COUNT(DISTINCT symbol) coins
               FROM signals WHERE ts >= ? AND ts < ? GROUP BY strategy""", (t0, t1)):
        reasons = db.query(
            """SELECT reject_reason, COUNT(*) n FROM signals
               WHERE ts >= ? AND ts < ? AND strategy = ? AND decision='rejected'
               GROUP BY reject_reason ORDER BY n DESC LIMIT 3""", (t0, t1, r["strategy"]))
        per_strategy.append({
            "strategy": r["strategy"], "signals": r["n"], "taken": r["taken"] or 0,
            "coins": r["coins"],
            "top_reject_reasons": [{"reason": (x["reject_reason"] or "")[:160], "n": x["n"]}
                                   for x in reasons],
        })
    per_strategy.sort(key=lambda x: -x["signals"])

    # ── what we actually did ──────────────────────────────────────────────────
    fills = db.query(
        """SELECT symbol, side, intent, strategy, notional_usd, fill_price,
                  mid_at_decision, ts_filled
           FROM orders WHERE ts_decided >= ? AND ts_decided < ? AND status='filled'
           ORDER BY ts_decided""", (t0, t1))
    # MODE MATTERS HERE. 2026-09-22: burst_catch was retired to mode='lab' --
    # out of the book, kept for the lab -- and this query had no mode filter, so
    # its 21 trades were still counted into the paper day. The operator's Daily
    # tab read -$42.65 when the book had actually lost $18.41: 40 trades instead
    # of 19, $44.11 of spread instead of $22.54. Everything a person reads as
    # "how did the desk do" has to be scoped to the mode the desk is running in;
    # the research paths (exit_lab, invariants, versions) deliberately are not.
    _mode = mode_mod.get_mode()
    closed = db.query(
        """SELECT symbol, strategy, entry_px, exit_px, qty, holding_s,
                  gross_pnl_usd, cost_usd, net_pnl_usd, ts_open, ts_close
           FROM trades WHERE ts_close >= ? AND ts_close < ? AND mode = ?
           ORDER BY ts_close""", (t0, t1, _mode))
    # (report_version is stamped into the summary at the end of build())
    net = sum(float(t["net_pnl_usd"] or 0) for t in closed)
    gross = sum(float(t["gross_pnl_usd"] or 0) for t in closed)
    cost = sum(float(t["cost_usd"] or 0) for t in closed)
    wins = sum(1 for t in closed if float(t["net_pnl_usd"] or 0) > 0)

    eq = db.query_one(
        "SELECT equity FROM equity_curve WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (t0, t1))

    # ── what it taught ────────────────────────────────────────────────────────
    avail = [c["best_from_open_pct"] for c in worth]
    lessons = []
    if uncovered_real:
        lessons.append(
            f"{len(uncovered_real)} coin(s) moved at least twice their own spread with NO "
            f"strategy watching (best {uncovered_real[0]['symbol']} "
            f"+{uncovered_real[0]['best_from_open_pct']:.1f}%). That is a real coverage gap.")
    if uncatchable:
        lessons.append(
            f"{len(uncatchable)} more were the slow-drift shape. Measured and left alone: "
            f"held-out gross -0.17% over 7,003 trades. It looks like a gap in hindsight "
            f"and has no forward edge.")
    thin = len(uncovered) - len(uncovered_real) - len(uncatchable)
    if thin > 0:
        lessons.append(
            f"A further {thin} cleared the spread only marginally (under 2x). Those need a "
            f"near-perfect exit to net anything and are not worth building a rule for.")
    if missed:
        lessons.append(
            f"{len(missed)} coin(s) were seen by a strategy but not traded — usually the "
            f"2-position cap or the one-position-per-coin rule.")
    if closed and gross > 0 > net:
        lessons.append(
            f"Direction was right and cost still won: the coins moved ${gross:+.2f} on the "
            f"mid and the spread took ${cost:.2f}.")
    if not coins:
        lessons.append("No bars stored for this day, so nothing can be judged.")

    return {
        "day": day,
        "built_at": time.time(),
        "counts": {
            "coins_scanned": len(coins),
            "worth_taking": len(worth),
            "workable": len(real),
            "uncovered": len(uncovered),
            "uncovered_workable": len(uncovered_real),
            "uncatchable": len(uncatchable),
            "missed": len(missed),
            "traded": len(traded),
            "orders_filled": len(fills),
            "trades_closed": len(closed),
        },
        # Which version of this code produced the row. get() rebuilds anything
        # stamped with an older one, so a fix reaches days that have gone quiet.
        "report_version": REPORT_VERSION,
        "pnl": {
            "net_usd": round(net, 4), "gross_usd": round(gross, 4),
            "cost_usd": round(cost, 4),
            "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "equity_end": float(eq["equity"]) if eq else None,
        },
        "opportunity": {
            "best_available_pct": round(max(avail), 3) if avail else 0.0,
            "median_available_pct": round(statistics.median(avail), 3) if avail else 0.0,
            "total_worth_taking": len(worth),
        },
        "uncovered": uncovered_real[:25],
        "uncovered_marginal": [c for c in uncovered if c["grade"] == "marginal"][:25],
        "uncatchable": uncatchable[:25],
        "missed": missed[:25],
        "movers": coins[:30],
        "strategies": per_strategy,
        "fills": [dict(f) for f in fills],
        "closed": [dict(c) for c in closed],
        "lessons": lessons,
    }


def _last_activity(day: str) -> float:
    """The newest thing that happened on this day, by the clock the DATA uses."""
    t0, t1 = _bounds(day)
    newest = 0.0
    _mode = mode_mod.get_mode()
    for sql, col in (("SELECT MAX(ts_close) m FROM trades "
                      "WHERE ts_close>=? AND ts_close<? AND mode=?", "m"),
                     ("SELECT MAX(ts_decided) m FROM orders "
                      "WHERE ts_decided>=? AND ts_decided<? AND mode=?", "m")):
        try:
            r = db.query_one(sql, (t0, t1, _mode))
            if r and r[col]:
                newest = max(newest, float(r[col]))
        except Exception:
            pass
    return newest


def get(day: str, force: bool = False) -> dict:
    ensure_schema()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    row = db.query_one("SELECT summary_json, built_at FROM daily_reports WHERE day=?", (day,))
    # A finished day was treated as final the moment it stopped being "today" --
    # but the report is built by a job that runs every six hours, so the last
    # build of a day happens at whatever time that job last fired DURING it, and
    # everything after that is never picked up.
    #
    # 2026-09-18: the report was built at 21:20 Austin; ENA closed at 22:49. The
    # Daily tab said 7 trades / +$36.73 for the rest of time while the Journal,
    # which reads the trades directly, showed 8 / +$42.29. Two tabs, two answers,
    # and the one that was wrong was the one that looked authoritative.
    #
    # Staleness is a question about the DATA, not the clock: rebuild when
    # something happened on that day after the report was built.
    stale = bool(row and _last_activity(day) > float(row["built_at"] or 0.0))
    # A row produced by an OLDER version of this code is stale too, however
    # quiet that day has been since. Without this, a fix to the report only
    # reaches days that happen to see new activity afterwards -- which for a
    # finished day is never.
    if row and not stale:
        try:
            if int((json.loads(row["summary_json"]) or {}).get("report_version") or 1) != REPORT_VERSION:
                stale = True
        except Exception:
            stale = True
    if row and not force and day != today and not stale:
        try:
            return json.loads(row["summary_json"])
        except Exception:
            pass
    # Today is rebuilt on demand, but not on EVERY demand. 2026-09-21: the
    # Daily tab (and its retries after a 20-second timeout) rebuilt today's
    # report on each request -- a GROUP BY over the day's bars plus two
    # queries per coin, all under the one database lock the engine's tick
    # was queueing for. Two minutes of staleness on a page that polls is
    # invisible; ten rebuilds a minute were not.
    # `not stale` matters here too. This throttle exists so a polling page does
    # not rebuild today's report on every request -- but it was returning the
    # cached row unconditionally, so a row known to be WRONG (built by older
    # code) was served anyway for as long as the throttle lasted. A throttle may
    # delay work; it must not serve an answer already established as stale.
    if (row and not force and not stale and day == today
            and (time.time() - float(row["built_at"] or 0.0)) < TODAY_REBUILD_S):
        try:
            return json.loads(row["summary_json"])
        except Exception:
            pass
    if stale:
        db.log_event("INFO", "report",
                     f"rebuilding {day}: stale (late activity, or built by an "
                     f"older version of the report)")
        try:
            from app.core import liveness
            liveness.fired("daily_report_rebuilt", day)
        except Exception:
            pass
    rep = build(day)
    db.execute(
        "INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET built_at=excluded.built_at, "
        "summary_json=excluded.summary_json",
        (day, rep["built_at"], json.dumps(rep)))
    return rep


def available_days(limit: int = 400) -> list[str]:
    """Days we hold bars for, newest first — what the date arrows can reach."""
    rows = db.query(
        # day-boundary-ok: which DAYS have bar coverage — a data-completeness figure.
        # A five-hour shift moves at most the first and last day of the span and
        # changes no decision; bucketing millions of bar rows in Python to be exact
        # would cost far more than it is worth.
        """SELECT DISTINCT date(ts, 'unixepoch', 'localtime') d FROM bars
           WHERE granularity = 900 ORDER BY d DESC LIMIT ?""", (limit,))
    return [r["d"] for r in rows if r["d"]]


def backfill(max_days: int = 60) -> dict:
    """Build every missing report, oldest first. Safe to run repeatedly."""
    ensure_schema()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    # ASK get(), DO NOT RE-DECIDE -- the THIRD copy of this decision, and the one
    # that kept the operator on a wrong number after two restarts. get() knows
    # when a row is stale: late activity, or built by an older version of this
    # code. This filter knew only the first, so a day whose MEANING had changed
    # was skipped here, skipped by series() (which had its own copy), and the
    # Daily tab served -$42.65 for a day the book had lost $18.41.
    #
    # get() is cheap on a fresh row -- it returns the stored JSON -- so every
    # candidate day is simply handed to it. One decision, one place. The only
    # judgement left here is which days EXIST, and `force` for today.
    built = []
    for d in sorted(available_days(max_days)):
        try:
            before = db.query_one("SELECT built_at FROM daily_reports WHERE day=?", (d,))
            get(d, force=(d == today))
            after = db.query_one("SELECT built_at FROM daily_reports WHERE day=?", (d,))
            if not before or (after and float(after["built_at"] or 0) != float(before["built_at"] or 0)):
                built.append(d)
        except Exception as exc:
            db.log_event("WARNING", "report", f"daily report for {d} failed: {exc}")
    return {"built": built, "count": len(built)}


def series(start: str, end: str) -> dict:
    """Aggregate a range: one row per day, for tables and charts."""
    ensure_schema()
    out = []
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    cur = d0
    while cur <= d1:
        day = cur.strftime("%Y-%m-%d")
        # ASK get(), DO NOT RE-DECIDE. This used to carry its own copy of the
        # staleness rule -- "rebuild if something happened after the report was
        # built" -- which was right when it was written and wrong the moment the
        # rule grew a second clause. On 2026-09-22 get() learned that a row built
        # by older code is stale too (REPORT_VERSION); this function did not, so
        # the single-day view served the corrected number and the range served
        # the old one. The Daily tab's totals are built from the RANGE, so the
        # operator restarted three times and kept seeing -$42.65.
        #
        # The comment this replaces said, in this very function: "a repair that
        # only reaches one caller is not a repair". The lesson was written down
        # and the duplication was left in place, which is how it happened again.
        # There is now ONE decision, in get(), and this asks it. get() is cheap
        # when the row is fresh -- it returns the stored JSON -- so calling it
        # unconditionally costs a query and removes a whole class of bug.
        try:
            get(day)
        except Exception:
            pass
        row = db.query_one("SELECT summary_json FROM daily_reports WHERE day=?", (day,))
        if row:
            try:
                r = json.loads(row["summary_json"])
                out.append({
                    "day": day,
                    "worth_taking": r["counts"]["worth_taking"],
                    "uncovered": r["counts"]["uncovered"],
                    "workable": r["counts"].get("workable", 0),
                    "uncovered_workable": r["counts"].get("uncovered_workable", 0),
                    "uncatchable": r["counts"].get("uncatchable", 0),
                    "missed": r["counts"]["missed"],
                    "traded": r["counts"]["traded"],
                    "trades_closed": r["counts"]["trades_closed"],
                    "net_usd": r["pnl"]["net_usd"],
                    "gross_usd": r["pnl"]["gross_usd"],
                    "cost_usd": r["pnl"]["cost_usd"],
                    "equity_end": r["pnl"]["equity_end"],
                    "best_available_pct": r["opportunity"]["best_available_pct"],
                })
            except Exception as exc:
                # A row that cannot be read used to vanish from the range in
                # silence -- dropping out of the per-day table AND out of the
                # totals, so the Daily tab would quietly under-report a week.
                # Say so instead: a missing day is a fact the operator can act
                # on, an invisible one is not.
                db.log_event("WARNING", "daily_report",
                             f"{day}: stored report unreadable, left out of the "
                             f"range ({type(exc).__name__}: {exc})")
        cur += timedelta(days=1)
    tot = {
        "days": len(out),
        "worth_taking": sum(x["worth_taking"] for x in out),
        "uncovered": sum(x["uncovered"] for x in out),
        "workable": sum(x.get("workable", 0) for x in out),
        "uncovered_workable": sum(x.get("uncovered_workable", 0) for x in out),
        "missed": sum(x["missed"] for x in out),
        "trades_closed": sum(x["trades_closed"] for x in out),
        "net_usd": round(sum(x["net_usd"] or 0 for x in out), 2),
        "gross_usd": round(sum(x["gross_usd"] or 0 for x in out), 2),
        "cost_usd": round(sum(x["cost_usd"] or 0 for x in out), 2),
    }
    # Coverage must be measured against what is actually CATCHABLE, otherwise it
    # rises whenever a shape is added to UNCATCHABLE rather than when a strategy
    # is built -- a metric that improves by redefining the target.
    catchable = tot["workable"] - sum(x.get("uncatchable", 0) for x in out)
    tot["catchable"] = catchable
    tot["coverage_pct"] = (round((catchable - tot["uncovered_workable"]) / catchable * 100, 1)
                           if catchable > 0 else None)
    return {"rows": out, "totals": tot}
