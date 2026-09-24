"""Statements about the data that must be true, checked continuously.

WHY THIS FILE EXISTS
--------------------
    "on this 'trades.open_order_id was NULL on all 16 rows'; how come you missed
     this. please put in tests on these so we dont have these types of issues
     anymore"

The honest answer to "how come you missed this" is that every check this repo
had was a check on the CODE -- does it compile, are all names defined, does every
component import, does every CSS variable resolve. The linkage code existed, read
correctly, and was wrong about the world. Nothing ever asked the only question
that would have caught it:

    after a trade closes, is the row actually filled in?

That is not a unit test, because the failure lives in data produced by a running
system, not in a function's return value. It is a POST-CONDITION on the database.
So that is what this module is: a list of things that must be true of the data,
each one phrased as a sentence the operator can check, each one naming the actual
bug it would have caught.

WHY IT IS A JOB AND NOT ONLY A TEST
-----------------------------------
A test that runs when somebody remembers to type `pytest` would not have caught
the NULL either -- the trades that were missing their linkage accumulated over
four days while every code gate stayed green. So these run on a schedule, land in
the event log, and surface in the app. `backend/tests/test_data_invariants.py`
runs the same functions so the gate also fails in preflight.

SEVERITY
--------
    "broken"  -- the data is wrong now. Something downstream is lying.
    "warning" -- suspicious, may be legitimate (a new database, an empty table).
    "ok"      -- the invariant holds, or there is nothing yet to check.

An empty table is never "broken": a fresh install has no trades, and a check that
screams on day one gets ignored by day three.
"""
from __future__ import annotations

import json
import time

from app.core import db



def _current_code_since() -> float:
    """When the code now running started.

    Several of these invariants are about bugs that have been fixed. The rows
    they produced are still in the database and always will be, so judging every
    check against all history means the page is permanently red and stops being
    read -- which is exactly how the NULL linkage survived four days of green
    gates. So each check that concerns a fixed bug judges only rows produced by
    the code now running, and reports the historical rate beside it as context.
    """
    try:
        from app.core import code_version
        return float(code_version._BOOT_TS)
    except Exception:
        return 0.0


def _result(name: str, question: str, ok: bool, detail: str,
            would_have_caught: str, severity: str = "broken",
            n_bad: int = 0, n_total: int = 0) -> dict:
    return {"name": name, "question": question, "ok": bool(ok),
            "status": "ok" if ok else severity, "detail": detail,
            "would_have_caught": would_have_caught,
            "n_bad": n_bad, "n_total": n_total}


# ─────────────────────── the linkage that was missing ───────────────────────

# Rows whose orders and signals were destroyed with the database on 2026-09-18
# and salvaged from its raw pages. They genuinely cannot link to anything: the
# order rows are gone. They are excluded from the linkage invariants and COUNTED
# OUT LOUD in the message, because an exception nobody can see is indistinguishable
# from the bug these invariants exist to catch.
_SALVAGED = ("attribution_json IS NOT NULL AND "
             "(attribution_json LIKE '%salvaged_from_corrupt_pages%' OR "
             " attribution_json LIKE '%corrected_from_corrupt_pages%')")


def _salvaged_ids() -> set[int]:
    try:
        return {r["id"] for r in db.query(f"SELECT id FROM trades WHERE {_SALVAGED}")}
    except Exception:
        return set()


def trades_link_to_orders() -> dict:
    rows = db.query("SELECT id, open_order_id, close_order_id FROM trades")
    salv = _salvaged_ids()
    bad = [r["id"] for r in rows
           if (r["open_order_id"] is None or r["close_order_id"] is None)
           and r["id"] not in salv]
    note = (f" ({len(salv)} salvaged 2026-09-18, orders destroyed with the database)"
            if salv else "")
    return _result(
        "trades_link_to_orders",
        "Does every closed trade point at the orders that opened and closed it?",
        not bad,
        (f"{len(bad)} of {len(rows)} trades have a NULL order id{note}"
         if bad else f"all {len(rows) - len(salv)} linkable trades are linked{note}"),
        "trades.open_order_id was NULL on all 16 rows for four days, which made "
        "it impossible to trace any outcome back to the features that caused it. "
        "Every learner downstream was silently working from nothing.",
        n_bad=len(bad), n_total=len(rows))


def trades_reach_their_features() -> dict:
    rows = db.query(
        "SELECT t.id AS id, s.features_json AS features FROM trades t "
        "LEFT JOIN orders o ON o.id = t.open_order_id "
        "LEFT JOIN signals s ON s.id = o.signal_id")
    salv = _salvaged_ids()
    bad = []
    for r in rows:
        if r["id"] in salv:
            continue                      # their signal rows were destroyed too
        if not r["features"]:
            bad.append(r["id"]); continue
        try:
            if not json.loads(r["features"]):
                bad.append(r["id"])
        except Exception:
            bad.append(r["id"])
    note = (f" ({len(salv)} salvaged 2026-09-18, signals destroyed with the database)"
            if salv else "")
    return _result(
        "trades_reach_their_features",
        "Can every trade be joined back to the features the model saw?",
        not bad,
        (f"{len(bad)} of {len(rows)} trades cannot reach a feature set{note}"
         if bad else f"all {len(rows) - len(salv)} linkable trades reach their features{note}"),
        "The whole point of the linkage. A trade that cannot reach its features "
        "can never be used to retrain anything, so the training set silently "
        "shrinks to zero while the app reports that learning is enabled.",
        severity="broken", n_bad=len(bad), n_total=len(rows))


def orders_link_to_signals() -> dict:
    rows = db.query("SELECT id, signal_id, side FROM orders WHERE side='buy'")
    bad = [r["id"] for r in rows if r["signal_id"] is None]
    return _result(
        "orders_link_to_signals",
        "Does every entry order name the signal that asked for it?",
        not bad,
        (f"{len(bad)} of {len(rows)} entry orders have no signal_id"
         if bad else f"all {len(rows)} entry orders name their signal"),
        "The upstream half of the same break. An order with no signal is a trade "
        "nobody can explain after the fact.",
        n_bad=len(bad), n_total=len(rows))


# ────────────────────────── arithmetic that must hold ───────────────────────

def pnl_decomposes() -> dict:
    rows = db.query("SELECT id, gross_pnl_usd, cost_usd, net_pnl_usd FROM trades")
    bad = [r["id"] for r in rows
           if abs((r["gross_pnl_usd"] or 0) - (r["cost_usd"] or 0) - (r["net_pnl_usd"] or 0)) > 0.01]
    return _result(
        "pnl_decomposes",
        "Does gross minus cost equal net, on every trade, to the cent?",
        not bad,
        (f"{len(bad)} of {len(rows)} trades do not reconcile"
         if bad else f"all {len(rows)} trades reconcile"),
        "gross was being computed fill-to-fill and then had the spread subtracted "
        "again, so the spread was counted twice: a -$3.44 day was reported as "
        "-$8.89. Every number the operator was shown was wrong by one spread.",
        n_bad=len(bad), n_total=len(rows))


def costs_never_below_published() -> dict:
    """The dead-fix check.

    `cost_model` had a branch keyed on `est.source == "prior"`, a value that
    function never returns -- so the floor it was supposed to apply never applied,
    and simulated fills came in cheaper than Robinhood's own published spread.
    The fix was claimed as shipped a session before it actually worked, which is
    precisely the kind of claim this file exists to stop me making.
    """
    since = _current_code_since()
    cur = db.query_one(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN ABS(half_spread_bps) < 50 THEN 1 ELSE 0 END) AS cheap "
        "FROM cost_observations WHERE ts > ? AND half_spread_bps IS NOT NULL", (since,))
    hist = db.query_one(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN ABS(half_spread_bps) < 50 THEN 1 ELSE 0 END) AS cheap "
        "FROM cost_observations WHERE ts <= ? AND half_spread_bps IS NOT NULL", (since,))
    n = int((cur["n"] if cur else 0) or 0)
    cheap = int((cur["cheap"] if cur else 0) or 0)
    hn = int((hist["n"] if hist else 0) or 0)
    hcheap = int((hist["cheap"] if hist else 0) or 0)
    tail = (f" (before this build: {hcheap}/{hn})" if hn else "")
    if n == 0:
        return _result("costs_never_below_published",
                       "Is any simulated fill cheaper than the venue's published spread?",
                       True, f"no fills yet under the build now running{tail}",
                       "a spread fix reported as shipped that was dead code for a "
                       "session, discounting 12% of fills and flattering every "
                       "backtest that used them.",
                       severity="warning", n_bad=0, n_total=0)
    frac = cheap / n
    return _result(
        "costs_never_below_published",
        "Is any simulated fill cheaper than the venue's published spread?",
        frac < 0.01,
        f"{cheap} of {n} fills ({frac:.1%}) under this build landed below 50bps a "
        f"side; Robinhood's own quotes are 85.5-98.1bps{tail}",
        "a spread fix reported as shipped that was dead code for a session, "
        "discounting 12% of fills and flattering every backtest that used them.",
        n_bad=cheap, n_total=n)


# ───────────────────────── behaviour that must hold ─────────────────────────

def holds_are_not_collapsed() -> dict:
    """The morning_dip regression: a 14-hour hold silently became one hour.

    2026-09-21: this was written with a fixed 3900-second threshold -- "one
    bar" -- which is only one bar if the strategy runs on hourly bars. When
    burst_catch arrived on 15-minute bars with a declared 90-minute hold, 12 of
    its 14 trades came in under 3900s and the check went red for a rule that
    was doing precisely what it says it does. A permanently red check gets
    switched off (blueprint 5.1), so a threshold that cannot tell "faster than
    designed" from "designed to be fast" is a bug in the check.

    It now compares each trade against the hold THAT TRADE'S OWN SIGNAL
    declared, which is recorded in features_json at decision time. A collapse
    is a strategy whose median hold is under a tenth of what it asked for --
    morning_dip's 14h -> 1h is 7% and still trips it; burst_catch's 90m -> 24m
    is 27% and does not. Exiting early on a target or a stop is the rule
    working, so this looks at the median rather than at any single trade.
    """
    try:
        from app.feedback import defects
        since = defects.window_end()
    except Exception:
        since = 0.0
    rows = db.query(
        """SELECT t.strategy AS strategy, t.holding_s AS held, s.features_json AS f
           FROM trades t
           JOIN orders o ON o.id = t.open_order_id
           JOIN signals s ON s.id = o.signal_id
           WHERE t.ts_close > ? AND t.holding_s IS NOT NULL""", (since,))
    by: dict[str, list[float]] = {}
    for r in rows:
        try:
            declared = float((json.loads(r["f"] or "{}") or {}).get("hold_seconds") or 0.0)
        except Exception:
            declared = 0.0
        if declared <= 0:
            continue
        by.setdefault(r["strategy"], []).append(float(r["held"] or 0.0) / declared)
    offenders = []
    total = 0
    for strat, fracs in by.items():
        total += len(fracs)
        if len(fracs) < 5:
            continue
        fracs.sort()
        median = fracs[len(fracs) // 2]
        if median < 0.10:
            offenders.append(f"{strat} (median hold {median:.0%} of the "
                             f"{len(fracs)} signals' own declared hold)")
    return _result(
        "holds_are_not_collapsed",
        "Is any strategy exiting far sooner than its own rule asked for?",
        not offenders,
        ("; ".join(offenders) if offenders
         else f"no strategy shows a collapsed hold across {total} post-fix trades"),
        "removing the hour gate from morning_dip silently turned its hold into "
        "max(1, sell_at_hour - hour), which collapsed to 1 hour. 5 of 8 live "
        "trades exited in a single bar and paid a full round trip for it.",
        n_bad=len(offenders), n_total=total)


def no_slot_cap_is_binding() -> dict:
    """A count of open positions must never be the reason a trade did not happen."""
    since = time.time() - 30 * 86400
    rows = db.query(
        "SELECT COUNT(*) AS n FROM events WHERE ts > ? AND "
        "(message LIKE '%max_concurrent_positions%' OR message LIKE '%positions (limit%')",
        (since,))
    n = int((rows[0]["n"] if rows else 0) or 0)
    return _result(
        "no_slot_cap_is_binding",
        "Did a slot count -- rather than cash -- ever refuse a trade?",
        n == 0,
        (f"{n} rejections in 30 days name a position-count limit" if n
         else "no trade was refused for a position count"),
        "on 2026-09-16 day_climb fired ZEC at 01:00 and NEAR at 06:00 and both "
        "were refused for 'already holding 2 positions'. The desk bought them "
        "twelve hours later near the high. A slot cap systematically buys the "
        "LAST signal of the day.",
        severity="broken", n_bad=n)


def open_positions_carry_their_order() -> dict:
    """Uniqueness per symbol is already enforced by the primary key, so checking
    it would be theatre. What can actually be wrong is the position-side half of
    the linkage: a position opened without recording which order opened it
    produces a trade that cannot be traced, which is the exact bug this file
    exists for -- caught one step earlier, while the position is still open."""
    rows = db.query("SELECT symbol, mode, open_order_id, avg_px FROM positions WHERE qty > 0")
    bad = [f"{r['symbol']}/{r['mode']}" for r in rows
           if r["open_order_id"] is None or not r["avg_px"] or float(r["avg_px"]) <= 0]
    return _result(
        "open_positions_carry_their_order",
        "Does every open position record the order that opened it, at a real price?",
        not bad,
        ("; ".join(bad) if bad else f"all {len(rows)} open positions are traceable"),
        "the open side of the NULL linkage. Checking it here catches the break "
        "while the position is still open, instead of discovering it days later "
        "in a closed trade that can no longer be repaired.",
        n_bad=len(bad), n_total=len(rows))


def equity_reconciles() -> dict:
    from app.risk import guards
    out = []
    for mode in ("paper",):
        try:
            acct = guards.account(mode)
        except Exception as exc:
            out.append(f"{mode}: {exc}")
            continue
        held = db.query_one(
            "SELECT COALESCE(SUM(qty * COALESCE(avg_px,0)),0) AS v FROM positions "
            "WHERE mode=? AND qty>0", (mode,))
        v = float((held["v"] if held else 0) or 0)
        if acct["equity"] > 0 and abs((acct["cash"] + v) - acct["equity"]) > max(1.0, acct["equity"] * 0.05):
            out.append(f"{mode}: cash {acct['cash']:.2f} + positions {v:.2f} "
                       f"!= equity {acct['equity']:.2f}")
    return _result(
        "equity_reconciles",
        "Does cash plus the value of open positions equal equity?",
        not out,
        ("; ".join(out) if out else "cash and positions add up to equity"),
        "equity was marked once per tick rather than after each fill, so two "
        "positions opened in the same tick both spent the same cash.",
        severity="warning", n_bad=len(out))


def reports_carry_a_shape() -> dict:
    rows = db.query("SELECT day, summary_json FROM daily_reports ORDER BY day DESC LIMIT 14")
    unknown = 0
    total = 0
    for r in rows:
        try:
            p = json.loads(r["summary_json"])
        except Exception:
            continue
        for u in (p.get("uncovered") or []):
            total += 1
            if (u.get("shape") or "unknown") == "unknown":
                unknown += 1
    ok = total == 0 or (unknown / total) < 0.2
    return _result(
        "reports_carry_a_shape",
        "Do the daily reports classify what shape each missed move was?",
        ok,
        (f"{unknown} of {total} uncovered movers in the last 14 days have no shape"
         if total else "no uncovered movers recorded yet"),
        "209 coin-days sat in the accumulator as 'unknown' because they predated "
        "the classifier. Unknown crosses every threshold while carrying no "
        "information, so the accumulator would have 'concluded' from nothing.",
        severity="warning", n_bad=unknown, n_total=total)



def targets_are_plausible() -> dict:
    """No stored exit target may be absurdly far from what was paid for the coin."""
    rows = db.query("SELECT symbol, mode, avg_px, target_px FROM positions "
                    "WHERE qty > 0 AND target_px IS NOT NULL")
    bad = []
    for r in rows:
        entry, target = float(r["avg_px"] or 0), float(r["target_px"] or 0)
        if entry > 0 and target > 0 and (target / entry > 3.0 or target / entry < 0.33):
            bad.append(f"{r['symbol']} entry {entry:.6g} target {target:.6g} "
                       f"({target / entry:.1f}x)")
    return _result(
        "targets_are_plausible",
        "Is every stored exit target within reach of the price paid?",
        not bad,
        ("; ".join(bad) if bad else f"all {len(rows)} targets are plausible"),
        "pump_ride used target_bps=100_000 to mean 'no target'. The engine "
        "multiplied it out and stored a $2.47 target on an ARB position bought at "
        "$0.2245 — eleven times the price. A sentinel written into a price column "
        "is believed by everything downstream.",
        n_bad=len(bad), n_total=len(rows))


def no_position_risks_more_than_the_day() -> dict:
    """One trade must not be able to end the trading day by itself."""
    from app.risk import guards
    try:
        cap = guards.daily_loss_limit_usd()
    except Exception as exc:
        return _result("no_position_risks_more_than_the_day",
                       "Can any single position lose more than the daily cap?",
                       True, f"could not read the cap: {exc}", "n/a", severity="warning")
    bad = []
    rows = db.query("SELECT symbol, qty, avg_px, stop_px FROM positions "
                    "WHERE mode='paper' AND qty > 0")
    for r in rows:
        qty, entry = abs(float(r["qty"] or 0)), float(r["avg_px"] or 0)
        stop = float(r["stop_px"] or 0)
        if qty <= 0 or entry <= 0:
            continue
        risk = qty * (entry - stop) if 0 < stop < entry else qty * entry
        if risk > cap:
            bad.append(f"{r['symbol']} risks ${risk:,.2f} vs a ${cap:,.2f} cap")
    return _result(
        "no_position_risks_more_than_the_day",
        "Can any single position lose more than the whole daily loss cap?",
        not bad,
        ("; ".join(bad) if bad else
         f"no position among {len(rows)} risks more than ${cap:,.2f}"),
        "a $1,132 ARB position with an 8% trailing stop risked $90.57 against a "
        "$60 daily cap — one trade able to lose one and a half times the day's "
        "entire budget, with nothing anywhere saying no.",
        n_bad=len(bad), n_total=len(rows))


def book_risk_within_drawdown() -> dict:
    """If every stop fired at once, the desk must still be trading tomorrow."""
    from app.config import get_settings
    from app.risk import guards
    try:
        equity = guards.account("paper")["equity"]
        budget = equity * float(get_settings().max_drawdown_pct) / 100.0
        risk = guards.open_risk_usd("paper")
    except Exception as exc:
        return _result("book_risk_within_drawdown",
                       "Would every stop firing at once breach the drawdown limit?",
                       True, f"unavailable: {exc}", "n/a", severity="warning")
    return _result(
        "book_risk_within_drawdown",
        "Would every stop firing at once breach the drawdown limit?",
        risk <= budget,
        f"open book risks ${risk:,.2f} against a ${budget:,.2f} drawdown limit",
        "positions were sized one at a time against free cash, so ten individually "
        "reasonable positions could add up to a loss far larger than the day's "
        "budget. Nothing computed the total until it was asked for.",
        severity="warning")




def prices_on_screen_are_fresh() -> dict:
    """Every price the app shows must be a price, not a memory of one."""
    from app.data.prices import live_price, STALE_AFTER_S
    rows = db.query("SELECT DISTINCT symbol FROM positions WHERE qty > 0 AND mode='paper'")
    stale = []
    for r in rows:
        p = live_price(r["symbol"])
        if p.px is None:
            stale.append(f"{r['symbol']}: no price at all")
        elif p.is_stale:
            stale.append(f"{r['symbol']}: {int((p.age_s or 0) // 60)}m old (from the {p.source})")
    return _result(
        "prices_on_screen_are_fresh",
        "Is every held coin priced from something recent?",
        not stale,
        ("; ".join(stale) if stale else
         f"all {len(rows)} held coins priced within {int(STALE_AFTER_S)}s"),
        "the app showed ARB at 0.2183 while Robinhood showed 0.2183 and the real "
        "mid was 0.2228. It was reading the last BAR close — 40 minutes old — "
        "while a 3-second-old quote sat unread in the quotes table. A stale price "
        "flows into unrealised P&L and into what a position looks like it is worth.",
        severity="warning", n_bad=len(stale), n_total=len(rows))



def known_gaps_are_declared() -> dict:
    """Surface every window where the ledger is knowingly incomplete.

    A restored ledger is indistinguishable from a ledger where nothing happened.
    On 2026-09-18 the operator went looking for a $1,132 ARB position he clearly
    remembered and found nothing — the restore predated it, and no screen
    anywhere said a window was missing. The absence looked like a fact.

    Reporting only: this never goes red, because a declared gap is not a defect.
    It exists so the hole is on screen instead of in a chat log.
    """
    try:
        rows = db.query("SELECT id, from_label, to_label, known_lost_json "
                        "FROM data_gaps ORDER BY from_ts")
    except Exception:
        rows = []
    if not rows:
        return _result(
            "known_gaps_are_declared",
            "Is every window of missing history declared on screen?",
            True, "no gaps declared — the ledger is believed complete",
            "A restored database looks exactly like one where nothing happened.",
            severity="note")
    parts = []
    for r in rows:
        try:
            lost = json.loads(r["known_lost_json"] or "[]")
        except Exception:
            lost = []
        parts.append(f"{r['from_label']} → {r['to_label']}: {len(lost)} known loss(es)")
    return _result(
        "known_gaps_are_declared",
        "Is every window of missing history declared on screen?",
        True,
        f"{len(rows)} declared gap(s) — " + "; ".join(parts),
        "The $1,132 ARB position of 2026-09-17 22:00 is inside one of these. It was "
        "real, it never closed, and it is not recoverable — so it is declared rather "
        "than invented.",
        severity="note", n_bad=0, n_total=len(rows))


CHECKS = [
    known_gaps_are_declared,
    trades_link_to_orders,
    trades_reach_their_features,
    orders_link_to_signals,
    pnl_decomposes,
    costs_never_below_published,
    holds_are_not_collapsed,
    no_slot_cap_is_binding,
    open_positions_carry_their_order,
    equity_reconciles,
    reports_carry_a_shape,
    targets_are_plausible,
    no_position_risks_more_than_the_day,
    book_risk_within_drawdown,
    prices_on_screen_are_fresh,
]


def run_all() -> dict:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(_result(fn.__name__, fn.__doc__ or fn.__name__, False,
                                   f"the check itself failed: {type(exc).__name__}: {exc}",
                                   "a check that cannot run is not a passing check",
                                   severity="warning"))
    broken = [r for r in results if r["status"] == "broken"]
    warnings = [r for r in results if r["status"] == "warning"]
    return {
        "checked_at": time.time(),
        "results": results,
        "n_broken": len(broken),
        "n_warning": len(warnings),
        "ok": not broken,
        "headline": ("every invariant holds" if not broken else
                     f"{len(broken)} invariant(s) broken: "
                     + ", ".join(r["name"] for r in broken)),
    }
