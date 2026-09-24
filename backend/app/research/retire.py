"""Take a strategy out of the book without losing what it taught us.

WHY THIS EXISTS -- 2026-09-21
-----------------------------
burst_catch was built on 09-20 from one afternoon's observation and put straight
into the paper book. In about six hours it took 17 trades, finished essentially
flat on price (gross -$3.95) and paid $17.68 in spread, for -$21.63 net -- while
the other six strategies together had made about $56 in nine days. The operator's
verdict: "we learned a lesson to not bring an untested strategy to either live
paper or real money."

So the fix is not a delete. A deleted trade teaches nothing, and the 17 trades
are the only real-fill evidence this rule will ever have from its first day.
What is wanted is a strategy that is OUT of the book and IN the lab:

    out of the book    no entries, and its P&L no longer counts toward paper
                       equity, the daily report, or any strategy's posterior
    in the lab         every trade, order and fill still there, still read by
                       exit_lab and every research path that does not filter on
                       mode -- so it can be tuned against what actually happened

Both come free from one change: move its rows from mode='paper' to mode='lab'.
`engine._mark_equity` sums `net_pnl_usd WHERE mode=?`, `guards.open_positions`
filters on mode, and `feedback.loop` scopes every posterior by mode -- so the
book heals itself on the next tick with no equity surgery. `exit_lab` reads
`FROM trades` with no mode filter, so the lab keeps all of it. Nothing is
deleted, nothing is recomputed by hand, and `restore()` puts it back.

WHAT THIS IS NOT
----------------
It is not the operator's switch (`risk/control.py`). That stops a strategy
trading in one click and is the thing to reach for in the moment. This is the
slower, heavier decision -- "this does not belong in the book at all" -- and it
sets the switch off as part of doing its job, so the two can never disagree.
"""
from __future__ import annotations

import time

from app.core import db

LAB_MODE = "lab"

# Tables that carry a `mode` column and therefore decide what the book is.
# Kept as data rather than three hand-written statements so a table that grows
# a mode column later is added in one place.
MODED_TABLES = ("trades", "orders", "positions")


def _counts(strategy: str, mode: str) -> dict:
    out: dict = {}
    for t in MODED_TABLES:
        row = db.query_one(f"SELECT COUNT(*) n FROM {t} WHERE strategy=? AND mode=?",
                           (strategy, mode))
        out[t] = int(row["n"] or 0) if row else 0
    return out


def preview(strategy: str, mode: str = "paper") -> dict:
    """Exactly what retiring would move, and what the book looks like after.

    Read-only. The operator sees this before anything changes, because "remove
    its trades" is the kind of instruction whose consequences should be on the
    screen before it is carried out, not described afterwards.
    """
    rec = db.query_one(
        """SELECT COUNT(*) n,
                  COALESCE(SUM(net_pnl_usd),0)   net,
                  COALESCE(SUM(gross_pnl_usd),0) gross,
                  COALESCE(SUM(cost_usd),0)      cost,
                  SUM(CASE WHEN net_pnl_usd>0 THEN 1 ELSE 0 END) wins
           FROM trades WHERE strategy=? AND mode=? AND ts_close IS NOT NULL""",
        (strategy, mode))
    book = db.query_one(
        "SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usd),0) net FROM trades WHERE mode=?",
        (mode,))
    opens = db.query(
        "SELECT symbol, qty, avg_px, opened_ts FROM positions "
        "WHERE strategy=? AND mode=? AND qty != 0", (strategy, mode))
    moved_net = float(rec["net"] or 0.0) if rec else 0.0
    book_net = float(book["net"] or 0.0) if book else 0.0
    return {
        "strategy": strategy,
        "mode": mode,
        "rows": _counts(strategy, mode),
        "closed_trades": int(rec["n"] or 0) if rec else 0,
        "wins": int(rec["wins"] or 0) if rec else 0,
        "net_usd": moved_net,
        "gross_usd": float(rec["gross"] or 0.0) if rec else 0.0,
        "cost_usd": float(rec["cost"] or 0.0) if rec else 0.0,
        "open_positions": [
            {"symbol": p["symbol"], "cost_basis_usd": float(p["qty"]) * float(p["avg_px"]),
             "opened_ts": p["opened_ts"]} for p in opens],
        "book_net_before_usd": book_net,
        "book_net_after_usd": book_net - moved_net,
        "book_trades_before": int(book["n"] or 0) if book else 0,
    }


def retire(strategy: str, reason: str, mode: str = "paper",
           close_open_first: bool = True) -> dict:
    """Move every row this strategy owns out of `mode` and into the lab.

    An OPEN position is closed first, at the mark, through the engine's own exit
    path -- never moved while open. Two reasons, and both matter more than the
    convenience of skipping it: the venue's holding is real (in live mode it is
    an actual coin balance), and a position sitting in a mode nothing marks is a
    position with nothing watching it, which is the exact failure the operator's
    switch was built to avoid.
    """
    before = preview(strategy, mode)
    closed: list[str] = []
    failed: list[str] = []
    if close_open_first and before["open_positions"]:
        from app.execution import engine
        from app.risk import guards as _g
        want = {p["symbol"] for p in before["open_positions"]}
        # engine.close_all() is the ONE path that closes a position properly --
        # live quote, _close_position, the trade row, the event. Reimplementing
        # a narrower version here would be a second exit path to keep correct,
        # so this drives that one and filters to this strategy's positions.
        for pos in _g.open_positions(mode):
            if pos["strategy"] != strategy or pos["symbol"] not in want:
                continue
            row = db.query_one("SELECT feed_product FROM universe WHERE symbol=?",
                               (pos["symbol"],))
            if not row:
                failed.append(f"{pos['symbol']} (not in the universe -- no feed to quote it)")
                continue
            try:
                q, _ = engine.live_quote(pos["symbol"], row["feed_product"])
                engine._close_position(pos, q, f"retiring {strategy}: {reason}", mode)
                closed.append(pos["symbol"])
            except Exception as exc:
                failed.append(f"{pos['symbol']} ({type(exc).__name__}: {str(exc)[:80]})")
        if failed:
            # Refuse rather than half-move. A partial retirement leaves the book
            # in a state no page describes correctly.
            return {"ok": False, "strategy": strategy,
                    "error": "could not close every open position; nothing was moved",
                    "closed": closed, "failed": failed, "preview": before}

    # Re-read after the closes: those positions have become trades.
    moved = _counts(strategy, mode)
    for t in MODED_TABLES:
        db.execute(f"UPDATE {t} SET mode=? WHERE strategy=? AND mode=?",
                   (LAB_MODE, strategy, mode))
    after = preview(strategy, mode)

    # Switch it off too, so the runtime switch and this can never disagree.
    try:
        from app.risk import control
        control.set_state(strategy, enabled=False,
                          note=f"retired to the lab: {reason}",
                          source=control.SOURCE_OPERATOR)
    except Exception:
        pass

    record = {"action": "retire", "strategy": strategy, "from_mode": mode,
              "to_mode": LAB_MODE, "reason": reason, "rows_moved": moved,
              "closed_on_retire": closed,
              "net_removed_from_book_usd": before["net_usd"],
              "book_net_before_usd": before["book_net_before_usd"],
              "book_net_after_usd": before["book_net_before_usd"] - before["net_usd"],
              "ts": time.time()}
    # Append-only first, then the journal: the vault is the copy that cannot be
    # quietly edited later, and "how much did retiring it change the book" is
    # exactly the number someone will want to check months from now.
    try:
        from app.core import vault
        vault.record("book_change", f"retire:{strategy}:{int(record['ts'])}", record)
    except Exception:
        pass
    try:
        from app.core import journal
        journal.append("book_change", record)
    except Exception:
        pass
    return {"ok": True, **record, "after": after}


def restore(strategy: str, mode: str = "paper") -> dict:
    """Put a retired strategy's rows back into `mode`. The exact inverse."""
    moved = _counts(strategy, LAB_MODE)
    for t in MODED_TABLES:
        db.execute(f"UPDATE {t} SET mode=? WHERE strategy=? AND mode=?",
                   (mode, strategy, LAB_MODE))
    record = {"action": "restore", "strategy": strategy, "from_mode": LAB_MODE,
              "to_mode": mode, "rows_moved": moved, "ts": time.time()}
    try:
        from app.core import vault
        vault.record("book_change", f"restore:{strategy}:{int(record['ts'])}", record)
    except Exception:
        pass
    try:
        from app.core import journal
        journal.append("book_change", record)
    except Exception:
        pass
    return {"ok": True, **record}


def retired(mode: str = "paper") -> list[dict]:
    """Strategies sitting in the lab, with what they took with them."""
    rows = db.query(
        """SELECT strategy, COUNT(*) n,
                  COALESCE(SUM(net_pnl_usd),0) net,
                  COALESCE(SUM(cost_usd),0) cost,
                  MAX(ts_close) last_close
           FROM trades WHERE mode=? GROUP BY strategy ORDER BY n DESC""",
        (LAB_MODE,))
    return [{"strategy": r["strategy"], "closed_trades": int(r["n"] or 0),
             "net_usd": float(r["net"] or 0.0), "cost_usd": float(r["cost"] or 0.0),
             "last_close_ts": r["last_close"]} for r in rows]
