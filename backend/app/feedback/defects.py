"""Trades produced by our own bugs, and why they are kept but not learned from.

The operator asked the right question: should the trades our defects produced be
archived so they stop poisoning the feedback loop?

Yes for LEARNING. No for P&L.

  * The money was really lost. Excluding a bad trade from the equity curve would
    be fabricating a better record than we have, so `trades`, `equity_curve` and
    every P&L number keep every row, defective or not.
  * But a strategy's measured performance is supposed to say whether its RULE
    works. Five of morning_dip's eight trades were forced out after one hour by a
    hold-calculation bug, not by its rule. Scoring the rule on those is scoring
    the bug, and the relearn loop would have paused a strategy for a defect the
    author introduced.

So each known defect is declared here with the window it was live in and a test
for which trades it touched. `relearn` skips them; the ledger keeps them; the
Journal shows why a row is excluded rather than hiding it.

Adding a defect is deliberately a code change, not a UI control. Excluding an
inconvenient trade should require writing down what the bug was.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")


CLOSED_KEY = "defect_window_closed_2026_09_16"


def _ts(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


def window_end() -> float:
    """When these defects actually stopped happening.

    A fix in a file changes nothing until the process restarts, so the window
    closes at the FIRST BOOT carrying the fix — stamped once, at boot, rather
    than guessed at here. Before that stamp exists, every trade is in the window.
    """
    try:
        from app.core import db
        row = db.query_one("SELECT value FROM app_state WHERE key=?", (CLOSED_KEY,))
        return float(row["value"]) if row and row["value"] else float("inf")
    except Exception:
        return float("inf")


def close_window_once() -> dict:
    """Called at boot. The first boot after the fix ends the defect window."""
    try:
        import time as _t
        from app.core import db
        if db.query_one("SELECT value FROM app_state WHERE key=?", (CLOSED_KEY,)):
            return {"closed": False, "reason": "already stamped"}
        now = _t.time()
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (CLOSED_KEY, str(now), now))
        db.log_event("INFO", "setup",
                     "defect window closed — trades from here on are scored normally")
        return {"closed": True, "at": now}
    except Exception as exc:
        return {"closed": False, "error": f"{type(exc).__name__}: {exc}"}


# A fix only becomes real at the RESTART that follows it, so these windows end at
# a boot time, not at the moment the file was edited.
DEFECTS = [
    {
        "key": "hold_collapse_1h",
        "title": "morning_dip forced out after one hour",
        "what": ("hold_hours was computed as max(1, sell_at_hour - hour). Once the "
                 "hour gate was removed, every entry after 13:00 local got a "
                 "one-hour hold — paying a 1.92% round trip to hold something for "
                 "an hour. Five of eight morning_dip trades were affected."),
        "fixed_live_at": None,          # set when the restart that carries the fix lands
        "applies": lambda t: (t["strategy"] == "morning_dip"
                              and (t["holding_s"] or 0) <= 2.05 * 3600),
    },
    {
        "key": "spread_randomised",
        "title": "paper fills drew the posted spread from a normal",
        "what": ("simulate_fill was meant to charge Robinhood's posted spread in "
                 "full, but the branch that did so tested for a source string "
                 "estimate() never returns. Every fill kept drawing from "
                 "Normal(95bps, 38bps): about one in eight cheaper than 50 bps. "
                 "Costs — and therefore net P&L — are understated on these."),
        "fixed_live_at": None,
        "applies": lambda t: True,      # windowed below, not per-trade
        # closed at the first boot carrying the fix; see window_end()
    },
]


def defects_for(trade: dict) -> list[dict]:
    """Which known defects touched this trade."""
    out = []
    for d in DEFECTS:
        if float(trade.get("ts_close") or 0) >= window_end():
            continue
        try:
            if d["applies"](trade):
                out.append({"key": d["key"], "title": d["title"], "what": d["what"]})
        except Exception:
            continue
    return out


def is_excluded(trade: dict) -> bool:
    return bool(defects_for(trade))


def summary(trades: list[dict]) -> dict:
    """How much of the record is defect-affected, per defect."""
    counts: dict[str, int] = {}
    for t in trades:
        for d in defects_for(t):
            counts[d["key"]] = counts.get(d["key"], 0) + 1
    excluded = sum(1 for t in trades if is_excluded(t))
    return {
        "total": len(trades),
        "excluded": excluded,
        "clean": len(trades) - excluded,
        "by_defect": counts,
        "note": ("Excluded from strategy scoring only. Every one of these trades "
                 "stays in the ledger, the equity curve and P&L — the money moved."),
    }
