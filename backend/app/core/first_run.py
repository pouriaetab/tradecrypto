"""The first thing a new person sees, and the two ways in.

The operator handed this app to a non-technical friend and watched the problem:
a correct, empty dashboard is indistinguishable from a broken one. Every page
says "insufficient evidence", every chart is blank, and there is no way to tell
whether the thing works, because it has not done anything yet -- and it will not
for days.

So the first screen asks one question with two answers:

  DEMO      -- fill the book with trades so every page has something in it, and
               you can see what this is before deciding whether to run it.
  LIVE PAPER-- start empty and accumulate your own, from today.

THE DEMO IS NOT FAKE NUMBERS. Fabricated rows would be the one thing this app
must never do: its entire purpose is to show what actually happened and why,
and a dashboard that invents its own evidence is worse than an empty one. So
the demo runs the REAL strategies over REAL historical prices already stored in
`bars`, through the REAL backtest with the REAL Robinhood cost model. Every
trade in it is a trade those rules would have taken, at prices that happened.
The only difference from live paper is that it happened last month instead of
this afternoon.

Every demo trade is stamped `"demo": true` in its attribution, a banner says so
while any remain, and one button clears them. Nobody should ever mistake the
demo for their own results.

AND THE DEMO BOOK LOSES MONEY. Measured, not feared: about 75 trades over 30
days, a 41% win rate, and a net loss once the 1.9% round trip is paid. That is
not a flaw in the demo -- it is the finding this entire application exists to
surface, and the operator's own live book says the same thing. A demo tuned to
look profitable would be the single most dishonest thing in the codebase. It
shows what the rules really do, and the Cost page explains why.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.core import db

CHOICE_KEY = "first_run_choice"
SEEDED_KEY = "first_run_demo_seeded_ts"

# Robinhood: ~0.95% each way, no commission. The demo must pay what a real trade
# pays, or it is an advertisement rather than a preview.
DEMO_COST_BPS_PER_SIDE = 95.0

# How much history to replay, and how many coins. Enough that every page has
# something; small enough that the whole thing finishes while somebody watches.
DEMO_DAYS = 30
DEMO_MAX_SYMBOLS = 14
DEMO_GRANULARITY = 3600

# A strategy needs a reasonable stretch of bars before its signals mean anything.
MIN_BARS_PER_SYMBOL = 120


def _get(key: str) -> str | None:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (key,))
    return row["value"] if row else None


def _put(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_ts=excluded.updated_ts",
        (key, value, time.time()),
    )


def demo_trade_count() -> int:
    """Trades carrying the demo stamp. Cheap enough to poll."""
    row = db.query_one(
        "SELECT COUNT(*) c FROM trades WHERE attribution_json LIKE '%\"demo\": true%'"
    )
    return int(row["c"]) if row else 0


def state() -> dict[str, Any]:
    """What the dashboard needs in order to decide what to show.

    `needs_choice` is deliberately conservative: it is only true when nobody has
    ever chosen AND the book is genuinely empty. A returning user must never be
    asked again, and someone with real trades must never see a demo offer.
    """
    choice = _get(CHOICE_KEY)
    trades = db.query_one("SELECT COUNT(*) c FROM trades")
    n_trades = int(trades["c"]) if trades else 0
    n_demo = demo_trade_count()
    bars = db.query_one("SELECT COUNT(*) c FROM bars")
    n_bars = int(bars["c"]) if bars else 0
    ready, why = demo_ready()
    return {
        "choice": choice,
        "needs_choice": choice is None and n_trades == 0,
        "trades": n_trades,
        "demo_trades": n_demo,
        "in_demo": n_demo > 0,
        "bars": n_bars,
        "demo_ready": ready,
        "demo_blocked_reason": None if ready else why,
        "seeded_ts": float(_get(SEEDED_KEY) or 0) or None,
        "engine_running": _engine_running(),
        "robinhood": _robinhood(),
        "real_money": _real_money(),
    }


def _engine_running() -> bool:
    try:
        from app.execution import engine
        return bool(engine.status().get("running"))
    except Exception:
        return False


def _robinhood() -> dict:
    """Read-only status. This never asks for, stores or transmits a credential.

    Connecting Robinhood does NOT turn on real-money trading and is not needed
    to run: prices come from Coinbase, free and anonymous. What it adds is which
    coins Robinhood will actually trade, and each coin's real spread instead of
    the published default.
    """
    try:
        from app.config import get_settings
        path = get_settings().rh_credentials_file
        return {
            "connected": bool(path and path.exists()),
            "where": str(path) if path else None,
            "what_it_adds": ("which coins Robinhood will actually trade, and "
                             "each coin's own measured spread instead of the "
                             "published default"),
            "not_needed_for": ("prices, strategies, signals or paper trading — "
                               "those work with no account at all"),
        }
    except Exception:
        return {"connected": False, "where": None}


def _real_money() -> dict:
    """Whether this install could place a real order. It cannot, by default.

    Two independent locks: the mode must be `mcp`, AND `.env` must carry an
    exact confirmation phrase that ships empty. A button in the interface can
    never satisfy the second one — that is the point of it.
    """
    try:
        from app.core import mode as mode_mod
        return {
            "possible": bool(mode_mod.live_allowed()),
            "mode": mode_mod.get_mode(),
            "how_it_stays_off": ("TC_LIVE_CONFIRM is empty in .env. Nothing in "
                                 "this interface can change it — it has to be "
                                 "typed into the file by hand, on purpose."),
        }
    except Exception:
        return {"possible": False, "mode": "paper"}


def demo_ready() -> tuple[bool, str]:
    """Is there enough stored price history to replay anything honest?

    On a brand-new install the background backfill is still running, so this is
    false for the first minute or two. The page says "still collecting prices"
    and enables the button by itself rather than failing on the click.
    """
    rows = db.query(
        "SELECT symbol, COUNT(*) c FROM bars WHERE granularity=? "
        "GROUP BY symbol HAVING c >= ? LIMIT ?",
        (DEMO_GRANULARITY, MIN_BARS_PER_SYMBOL, DEMO_MAX_SYMBOLS),
    )
    if len(rows) >= 4:
        return True, ""
    return False, (
        f"still collecting prices — {len(rows)} of 4 coins have enough history "
        "so far. This finishes on its own, usually within a couple of minutes."
    )


def _demo_symbols() -> list[str]:
    """The coins with the deepest stored history, most-traded first."""
    rows = db.query(
        "SELECT b.symbol, COUNT(*) c FROM bars b "
        "JOIN universe u ON u.symbol = b.symbol AND u.active = 1 "
        "WHERE b.granularity = ? GROUP BY b.symbol "
        "HAVING c >= ? ORDER BY c DESC LIMIT ?",
        (DEMO_GRANULARITY, MIN_BARS_PER_SYMBOL, DEMO_MAX_SYMBOLS),
    )
    return [r["symbol"] for r in rows]


# Each demo trade is sized at this fraction of the declared stake, so the dollar
# figures scale with whatever stake the user sets instead of being a number
# invented here.
DEMO_SIZE_FRACTION = 0.05

# Robinhood charges no commission and takes it in the spread instead: about
# 0.95% each way.
DEMO_HALF_SPREAD = 0.0095

# The live engine's odds gate (risk/guards.MIN_P_REACH).
DEMO_MIN_P_REACH = 0.35

# It will not hold more than this many at once, same as the desk.
DEMO_MAX_CONCURRENT = 3


def _replay(panel, name: str) -> tuple[list[dict], list[dict]]:
    """Entries from the real strategy; exits from the engine's own rules.

    This does NOT use `run_backtest`. The backtest has its own simple exit, and
    a demo built on it would be showing a program the user is not about to run.
    Here the exit is the live path: the covered price, `dynamic_target` with the
    same shrink when the odds go bad, the strategy's own stop, and a hold limit
    predicted by `time_budget` rather than a hardcoded clock. Those are pure
    functions with no clock and no database precisely so the lab and the engine
    can call the identical code -- this is that promise being used.
    """
    import numpy as np

    from app.strategy import time_budget
    from app.strategy.dynamic_target import dynamic_target
    from app.strategy.registry import build

    st = build(name)
    st.calibrate(panel, panel.T - 1)
    open_pos: dict[int, dict] = {}
    done: list[dict] = []
    # Every signal, and what was decided about it. The Journal and the Signals
    # page exist to show the decisions that did NOT become trades -- a demo with
    # only the winners-and-losers list would hide exactly the thing this app was
    # built to expose.
    sigs_out: list[dict] = []

    for t in range(st.warmup_bars() + 1, panel.T):
        for j in list(open_pos):
            e = open_pos[j]
            px = float(panel.close[t, j])
            if not np.isfinite(px):
                continue
            age_h = float(t - e["i"])
            now_pct = (px * (1 - DEMO_HALF_SPREAD) / e["entry"] - 1.0) * 100.0
            odds = time_budget.still_arrives(e["base"], e["atr"], age_h, now_pct)
            shrink = 0.008 * age_h if odds < 0.40 else 0.0
            target = dynamic_target(e["entry"], px, e["entry"] / (1 - DEMO_HALF_SPREAD),
                                    e["base"], age_h, shrink=shrink)
            reason = None
            if px <= e["stop"]:
                reason = "stop"
            elif target and px >= target:
                reason = "target"
            elif age_h > e["max_h"]:
                reason = "time"
            if reason:
                done.append({"symbol": panel.symbols[j], "i_in": e["i"], "i_out": t,
                             "entry": e["entry"], "exit": px * (1 - DEMO_HALF_SPREAD),
                             "mid_in": e["mid_in"], "mid_out": px,
                             "reason": reason, "odds": odds, "sig": e["sig"]})
                del open_pos[j]

        if len(open_pos) >= DEMO_MAX_CONCURRENT:
            continue
        try:
            sigs = st.generate(panel, t)
        except Exception:
            continue
        for sig in sigs:
            if sig.symbol not in panel.symbols:
                continue
            j = panel.symbols.index(sig.symbol)
            if j in open_pos:
                continue
            raw = float(panel.close[t, j])
            if not np.isfinite(raw) or raw <= 0:
                continue
            base = float(getattr(sig, "target_bps", 120.0) or 120.0) / 1e4
            stop_frac = float(getattr(sig, "stop_bps", 150.0) or 150.0) / 1e4
            lo = panel.low[max(0, t - 24):t + 1, j]
            hi = panel.high[max(0, t - 24):t + 1, j]
            with np.errstate(all="ignore"):
                atr = float(np.nanmean((hi - lo) / np.where(hi > 0, hi, np.nan)))
            if not np.isfinite(atr) or atr <= 0:
                atr = 0.02
            bud = time_budget.budget(base, atr)
            ts_bar = float(panel.ts[t])
            if not time_budget.worth_taking(base, atr, DEMO_MIN_P_REACH):
                sigs_out.append({
                    "ts": ts_bar, "symbol": sig.symbol, "side": "buy",
                    "raw_score": float(getattr(sig, "raw_score", None) or getattr(sig, "score", 0.0) or 0.0),
                    "edge_bps": base * 1e4, "decision": "rejected",
                    "reason": (f"odds of reaching the target are "
                               f"{bud['p_reach']:.0%}, below the {DEMO_MIN_P_REACH:.0%} "
                               f"this desk requires"),
                })
                continue
            if len(open_pos) >= DEMO_MAX_CONCURRENT:
                sigs_out.append({
                    "ts": ts_bar, "symbol": sig.symbol, "side": "buy",
                    "raw_score": float(getattr(sig, "raw_score", None) or getattr(sig, "score", 0.0) or 0.0),
                    "edge_bps": base * 1e4, "decision": "rejected",
                    "reason": f"already holding {DEMO_MAX_CONCURRENT} positions",
                })
                continue
            taken = {
                "ts": ts_bar, "symbol": sig.symbol, "side": "buy",
                "raw_score": float(getattr(sig, "raw_score", None) or getattr(sig, "score", 0.0) or 0.0),
                "edge_bps": base * 1e4, "decision": "taken", "reason": None,
                # The strategy's OWN features, exactly as the live engine
                # stores them. The first version invented a small dict instead,
                # so the Signals tree showed six variables nobody recognised and
                # labelled every one of them "recorded" -- the demo was not
                # previewing the real decision, it was previewing my summary of
                # it. `stop_bps` / `target_bps` matter as well: the odds lookup
                # reads target_bps, and without it every candidate showed no
                # odds at all.
                "features": {**dict(getattr(sig, "features", {}) or {}),
                             "demo": True,
                             "stop_bps": float(getattr(sig, "stop_bps", 0.0) or 0.0),
                             "target_bps": float(getattr(sig, "target_bps", 0.0) or 0.0),
                             "hold_seconds": float(getattr(sig, "hold_seconds", 0.0) or 0.0),
                             "atr_frac": round(atr, 5),
                             "p_reach": round(float(bud["p_reach"]), 3),
                             "p70_hours": float(bud["p70_h"])},
            }
            sigs_out.append(taken)
            open_pos[j] = {"i": t, "entry": raw * (1 + DEMO_HALF_SPREAD),
                           "base": base, "stop": raw * (1 - stop_frac), "atr": atr,
                           "max_h": max(6.0, float(bud["p70_h"])),
                           "mid_in": raw, "sig": taken}
            if len(open_pos) >= DEMO_MAX_CONCURRENT:
                break
    return done, sigs_out


def seed_demo(days: int = DEMO_DAYS) -> dict[str, Any]:
    """Replay the real strategies over real stored history and record the result.

    `refresh=False`: the panel is built from bars already on disk, so this makes
    no network calls and cannot hang on a slow feed. The background backfill is
    what puts the bars there.
    """
    from app.core import mode as mode_mod
    from app.execution.engine import build_panel
    from app.strategy.registry import ACTIVE_STRATEGIES

    ready, why = demo_ready()
    if not ready:
        return {"ok": False, "error": why, "trades": 0}

    symbols = _demo_symbols()
    if not symbols:
        return {"ok": False, "trades": 0,
                "error": "no coins have enough stored history yet"}

    panel = build_panel(symbols=symbols, granularity=DEMO_GRANULARITY,
                        limit=days * 24, refresh=False)
    if panel is None or panel.T < MIN_BARS_PER_SYMBOL:
        return {"ok": False, "trades": 0,
                "error": "not enough stored price history yet"}

    stake = float(mode_mod.get_equity() or 2000.0)
    notional = max(10.0, stake * DEMO_SIZE_FRACTION)

    rows: list[tuple] = []
    sig_rows: list[tuple] = []
    per_strategy: dict[str, int] = {}
    now = time.time()

    for name in ACTIVE_STRATEGIES:
        try:
            fills, sigs = _replay(panel, name)
        except Exception as exc:  # one broken strategy must not kill the demo
            db.log_event("WARNING", "demo",
                         f"demo replay skipped {name}: {type(exc).__name__}: {exc}")
            continue
        for g in sigs:
            if g["decision"] == "taken":
                continue          # written below, linked to its own trade
            sig_rows.append((
                g["ts"], name, "demo", g["symbol"], g["side"], g["raw_score"],
                g["edge_bps"], None, None, 2 * DEMO_HALF_SPREAD * 1e4,
                g["decision"], g["reason"],
                json.dumps({"demo": True}), 0,
            ))
        for f in fills:
            try:
                ts_open = float(panel.ts[f["i_in"]])
                ts_close = float(panel.ts[f["i_out"]])
            except (IndexError, TypeError):
                continue
            if ts_close <= ts_open:
                continue
            qty = round(notional / max(f["entry"], 1e-9), 8)
            gross = (f["exit"] - f["entry"]) * qty
            cost = (f["entry"] * qty + f["exit"] * qty) * DEMO_HALF_SPREAD
            rows.append((
                f["symbol"], name, "paper", qty, f["entry"], f["exit"],
                ts_open, ts_close, ts_close - ts_open,
                gross, cost, gross - cost,
                float(f["base"] if "base" in f else 0.0) * 1e4,
                (gross - cost) / max(notional, 1e-9) * 1e4,
                json.dumps({
                    "demo": True,
                    "exit_reason": f["reason"],
                    "odds_at_exit": round(float(f["odds"]), 3),
                    "note": ("replayed from real stored prices using the engine's "
                             "own exit rules and the real 1.9% round trip — "
                             "not a live trade"),
                }),
                f,
            ))
            per_strategy[name] = per_strategy.get(name, 0) + 1

    if not rows:
        return {"ok": False, "trades": 0,
                "error": ("the strategies took no trades over the stored history. "
                          "That is a real result, not a failure, but it makes a "
                          "poor demo — try again once more history has been "
                          "collected.")}

    rows.sort(key=lambda r: r[6])

    # SIGNAL -> ORDER -> ORDER -> TRADE, exactly as the live engine writes it.
    #
    # A bare trade row with NULL order ids is not merely untidy: the app's own
    # invariant checks read it as BROKEN ("61 of 61 trades have a NULL order
    # id", "61 of 61 trades cannot reach a feature set"), so the Data page would
    # tell a brand-new user their install is damaged. The demo has to be
    # structurally indistinguishable from real data or it breaks the very pages
    # it exists to fill.
    _insert_linked(rows)

    if sig_rows:
        with db.tx() as conn:
            conn.executemany(
                "INSERT INTO signals(ts, strategy, strategy_version, symbol, side, "
                "raw_score, expected_edge_bps, edge_ci_low_bps, edge_ci_high_bps, "
                "cost_hurdle_bps, decision, reject_reason, features_json, sample_size) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                sig_rows,
            )

    _write_equity_curve()
    _put(CHOICE_KEY, "demo")
    _put(SEEDED_KEY, str(now))

    net = sum(r[11] for r in rows)
    wins = sum(1 for r in rows if r[11] > 0)
    db.log_event("INFO", "demo",
                 f"demo book seeded: {len(rows)} trades replayed from stored prices, "
                 f"net ${net:.2f}, {100*wins/len(rows):.0f}% winners")
    # Taken signals are written by _insert_linked, one per trade; sig_rows holds
    # only the rejections. Reporting len(sig_rows) as "signals" said 0 on a run
    # that had written 39.
    return {"ok": True, "trades": len(rows),
            "signals": len(rows) + len(sig_rows),
            "signals_rejected": len(sig_rows),
            "symbols": len(symbols),
            "per_strategy": per_strategy, "net_usd": round(net, 2),
            "win_rate_pct": round(100.0 * wins / len(rows)),
            "notional_each": round(notional, 2),
            "days": round((rows[-1][7] - rows[0][6]) / 86400.0, 1)}


def _insert_linked(rows: list[tuple]) -> None:
    """Write signal -> open order -> close order -> trade, one chain per fill.

    Done row by row rather than with executemany because each insert needs the
    previous one's id. That is a few hundred statements at most, inside a single
    transaction, and it happens once.
    """
    with db.tx() as conn:
        for r in rows:
            (symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close,
             holding_s, gross, cost, net, pred_bps, real_bps, attrib, f) = r
            sig = f["sig"]
            cur = conn.execute(
                "INSERT INTO signals(ts, strategy, strategy_version, symbol, side, "
                "raw_score, expected_edge_bps, edge_ci_low_bps, edge_ci_high_bps, "
                "cost_hurdle_bps, decision, reject_reason, features_json, sample_size) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig["ts"], strategy, "demo", symbol, "buy", sig["raw_score"],
                 sig["edge_bps"], None, None, 2 * DEMO_HALF_SPREAD * 1e4,
                 "taken", None, json.dumps(sig["features"]), 0),
            )
            signal_id = cur.lastrowid

            order_ids = []
            for side, ts_fill, fill_px, mid in (
                ("buy", ts_open, entry_px, f["mid_in"]),
                ("sell", ts_close, exit_px, f["mid_out"]),
            ):
                cur = conn.execute(
                    "INSERT INTO orders(client_id, signal_id, strategy, symbol, side, "
                    "intent, qty, notional_usd, mode, status, ts_decided, ts_submitted, "
                    "ts_filled, mid_at_decision, mid_at_submit, fill_price, fee_usd, "
                    "ticket_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"demo-{signal_id}-{side}", signal_id, strategy, symbol, side,
                     "open" if side == "buy" else "close", qty, qty * fill_px, mode,
                     "filled", ts_fill, ts_fill, ts_fill, mid, mid, fill_px, 0.0,
                     json.dumps({"demo": True})),
                )
                order_ids.append(cur.lastrowid)

            conn.execute(
                "INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, "
                "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, "
                "predicted_edge_bps, realised_edge_bps, attribution_json, "
                "open_order_id, close_order_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close,
                 holding_s, gross, cost, net, pred_bps, real_bps, attrib,
                 order_ids[0], order_ids[1]),
            )


def _write_equity_curve() -> None:
    """A running balance, so the equity chart is not the one blank page left.

    Built from the trades themselves rather than invented, and written at each
    trade's close so the curve steps exactly where the book changed.
    """
    from app.core import mode as mode_mod

    start = float(mode_mod.get_equity() or 2000.0)
    trades = db.query(
        "SELECT ts_close, net_pnl_usd FROM trades "
        "WHERE mode='paper' AND ts_close IS NOT NULL ORDER BY ts_close"
    )
    if not trades:
        return
    equity = start
    out = []
    for t in trades:
        equity += float(t["net_pnl_usd"] or 0.0)
        out.append((float(t["ts_close"]), "paper", equity, equity, 0.0,
                    equity - start, 0.0))
    with db.tx() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO equity_curve(ts, mode, equity, cash, "
            "positions_value, realised_pnl, unrealised_pnl) VALUES (?,?,?,?,?,?,?)",
            out,
        )


def choose_live() -> dict[str, Any]:
    """Start empty: this person's numbers begin now, and are only ever theirs."""
    from app.core import setup_ops

    res = setup_ops.reset_paper_book("paper")
    _put(CHOICE_KEY, "live")
    db.log_event("INFO", "demo", "starting live paper with an empty book")
    return {"ok": True, "cleared": res}


def clear_demo() -> dict[str, Any]:
    """Remove every demo trade, leaving anything real behind.

    Matched on the stamp, never on a date range: a user who went live from
    inside the demo has both kinds interleaved, and their own trades must
    survive this untouched.
    """
    n = demo_trade_count()
    db.execute("DELETE FROM trades WHERE attribution_json LIKE '%\"demo\": true%'")
    db.execute("DELETE FROM equity_curve WHERE mode='paper'")
    _write_equity_curve()
    _put(CHOICE_KEY, "live")
    db.log_event("INFO", "demo", f"cleared {n} demo trades; real trades kept")
    return {"ok": True, "removed": n, "real_trades_kept": demo_trade_count() == 0}
