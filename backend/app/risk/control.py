"""Per-strategy on/off switch and spend cap, settable from the app.

WHY THIS EXISTS
---------------
2026-09-21. burst_catch was added the day before and lost $14.39 net across 14
trades inside about three hours -- roughly a quarter of everything the book had
made in nine days. The operator watched it happen and had no way to stop it
without asking me to edit a Python list, and he had run out of tokens to ask
with. That is the failure this module fixes: a bad strategy must be stoppable
by the person watching it, in the app, in one click, with no engineer in the
loop and no restart.

WHAT IT IS
----------
A row per strategy in `strategy_control`, read through `state()` and enforced in
`guards.pre_trade_check`. Three operator-settable fields:

    enabled          off means no NEW positions. Open positions still manage
                     themselves to their own exits -- turning a strategy off
                     must never strand money in a position with nothing
                     watching it. That distinction is the whole safety story
                     here, so it is enforced by intent: only intent="open" is
                     ever refused.
    daily_budget_usd cap on notional OPENED per Austin day. 0 = no cap.
                     Counted from filled open orders, not from intentions.
    max_position_usd per-trade ceiling for this strategy. 0 = no cap.

Everything is a DEFAULT-OPEN: a strategy with no row behaves exactly as it did
before this module existed. A missing row is not "disabled", it is "unmanaged".
This matters because ACTIVE_STRATEGIES is still the roster; this is a veto on
top of it, never a second source of truth about what exists.

WHAT IT DELIBERATELY IS NOT
---------------------------
It is not the allocator. Ranking strategies and moving cash between them is a
separate, automatic thing that has to be measured before it is trusted. This is
the manual override that sits ABOVE any allocator -- if the operator switches a
strategy off, no ranking may switch it back on. `set_state` records who made
the change (`source`) precisely so an automatic caller can never overwrite a
manual one without that being visible.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core import db

TZ = ZoneInfo("America/Chicago")

# An operator switch outranks an automatic one. Anything that is not "operator"
# is advisory and may be overridden by the operator at any time; "operator" may
# only be changed by another operator action.
SOURCE_OPERATOR = "operator"
SOURCE_AUTO = "auto"


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_control (
        strategy          TEXT PRIMARY KEY,
        enabled           INTEGER NOT NULL DEFAULT 1,
        daily_budget_usd  REAL    NOT NULL DEFAULT 0,
        max_position_usd  REAL    NOT NULL DEFAULT 0,
        note              TEXT,
        source            TEXT    NOT NULL DEFAULT 'operator',
        updated_ts        REAL    NOT NULL
    )""")


def _day_start(now: float | None = None) -> float:
    d = datetime.fromtimestamp(now or time.time(), TZ)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def state(strategy: str) -> dict:
    """Current control row, or the default-open shape when there is none."""
    try:
        row = db.query_one("SELECT * FROM strategy_control WHERE strategy=?", (strategy,))
    except Exception:
        row = None
    if not row:
        return {"strategy": strategy, "enabled": True, "daily_budget_usd": 0.0,
                "max_position_usd": 0.0, "note": None, "source": None,
                "managed": False, "updated_ts": None}
    return {"strategy": strategy,
            "enabled": bool(row["enabled"]),
            "daily_budget_usd": float(row["daily_budget_usd"] or 0.0),
            "max_position_usd": float(row["max_position_usd"] or 0.0),
            "note": row["note"], "source": row["source"],
            "managed": True, "updated_ts": row["updated_ts"]}


def spent_today(strategy: str, mode: str) -> float:
    """Notional this strategy has actually OPENED today, from filled orders.

    Counted from fills, not from decisions: an order that was refused or never
    filled did not spend anything, and a budget that counts intentions would
    lock a strategy out over trades that never happened.
    """
    row = db.query_one(
        """SELECT COALESCE(SUM(notional_usd), 0) v FROM orders
           WHERE strategy=? AND mode=? AND intent='open' AND status='filled'
             AND COALESCE(ts_filled, ts_submitted, ts_decided) >= ?""",
        (strategy, mode, _day_start()))
    return float(row["v"] if row else 0.0)


def check(strategy: str | None, notional_usd: float, mode: str,
          intent: str = "open") -> list[str]:
    """Reasons this strategy may not open this trade. Empty list means go.

    Never raises: a control table that cannot be read must not stop the desk
    trading. It fails OPEN, and the liveness registry is what catches a switch
    that has silently stopped being consulted.
    """
    if not strategy or intent != "open":
        return []
    try:
        st = state(strategy)
    except Exception:
        return []
    out: list[str] = []
    if not st["enabled"]:
        note = f" -- {st['note']}" if st.get("note") else ""
        out.append(f"{strategy} is switched off in the app{note}. Open positions "
                   f"keep managing themselves to their own exits; only new "
                   f"entries are refused.")
    cap = st["max_position_usd"]
    if cap > 0 and notional_usd > cap:
        out.append(f"${notional_usd:.2f} is over {strategy}'s per-trade cap of "
                   f"${cap:.2f}, set in the app")
    budget = st["daily_budget_usd"]
    if budget > 0:
        try:
            spent = spent_today(strategy, mode)
        except Exception:
            spent = 0.0
        if spent + notional_usd > budget:
            out.append(f"{strategy} has opened ${spent:.2f} of its ${budget:.2f} "
                       f"daily budget; this ${notional_usd:.2f} order would pass it")
    return out


def set_state(strategy: str, *, enabled: bool | None = None,
              daily_budget_usd: float | None = None,
              max_position_usd: float | None = None,
              note: str | None = None,
              source: str = SOURCE_OPERATOR) -> dict:
    """Write a control row. Only the fields passed are changed.

    An automatic caller (source != operator) may not touch a row an operator
    last wrote. The operator's hand is the final word on whether a strategy
    trades, and an allocator quietly re-enabling something he switched off
    would be exactly the kind of invisible override this app exists to not do.
    """
    ensure_schema()
    cur = state(strategy)
    if (source != SOURCE_OPERATOR and cur.get("managed")
            and cur.get("source") == SOURCE_OPERATOR):
        return {**cur, "refused": f"{strategy} was last set by the operator; "
                                  f"{source} may not change it"}
    nxt = {
        "enabled": cur["enabled"] if enabled is None else bool(enabled),
        "daily_budget_usd": cur["daily_budget_usd"] if daily_budget_usd is None
                            else max(0.0, float(daily_budget_usd)),
        "max_position_usd": cur["max_position_usd"] if max_position_usd is None
                            else max(0.0, float(max_position_usd)),
        "note": cur["note"] if note is None else (note or None),
    }
    db.execute(
        """INSERT INTO strategy_control
             (strategy, enabled, daily_budget_usd, max_position_usd, note, source, updated_ts)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(strategy) DO UPDATE SET
             enabled=excluded.enabled, daily_budget_usd=excluded.daily_budget_usd,
             max_position_usd=excluded.max_position_usd, note=excluded.note,
             source=excluded.source, updated_ts=excluded.updated_ts""",
        (strategy, 1 if nxt["enabled"] else 0, nxt["daily_budget_usd"],
         nxt["max_position_usd"], nxt["note"], source, time.time()))
    try:
        from app.core import liveness
        liveness.fired("strategy_control", f"{strategy} set by {source}")
    except Exception:
        pass
    return state(strategy)


def roster(mode: str = "paper") -> list[dict]:
    """Every strategy the operator could need to act on -- not just the active list.

    2026-09-21, and this is the bug worth remembering: burst_catch was taken off
    ACTIVE_STRATEGIES because it was being retired, and it immediately vanished
    from the control card -- while it still held 17 trades, 4 open positions and
    -$21.63 of the paper book. The one strategy that needed retiring was the one
    the retire button no longer listed.

    A roster built from the code's idea of what is "active" answers the wrong
    question. What the operator needs listed is everything with a claim on the
    book or a setting of its own:

        active      on ACTIVE_STRATEGIES -- it can still open positions
        off-roster  removed from the code's list but STILL HOLDING trades or
                    positions in this mode; the state that needs acting on
        in the lab  its rows have been moved out; listed so it can be restored

    Union, not intersection. A strategy disappears from this list only when it
    has nothing left here at all.
    """
    from app.strategy.registry import ACTIVE_STRATEGIES, RETIRED
    names: list[str] = list(ACTIVE_STRATEGIES)
    seen = set(names)

    def add(n):
        if n and n not in seen:
            seen.add(n)
            names.append(n)

    # Anything with money in this mode, a row in the lab, or a setting of its own.
    for sql, params in (
        ("SELECT DISTINCT strategy FROM trades WHERE mode=?", (mode,)),
        ("SELECT DISTINCT strategy FROM positions WHERE mode=? AND qty != 0", (mode,)),
        ("SELECT DISTINCT strategy FROM trades WHERE mode='lab'", ()),
        ("SELECT strategy FROM strategy_control", ()),
    ):
        try:
            for r in db.query(sql, params):
                add(r["strategy"])
        except Exception:
            # A table that does not exist yet (strategy_control before its first
            # write) must not empty the roster.
            continue

    out = []
    for name in names:
        st = state(name)
        try:
            st["spent_today_usd"] = spent_today(name, mode)
        except Exception:
            st["spent_today_usd"] = 0.0
        st["budget_left_usd"] = (max(0.0, st["daily_budget_usd"] - st["spent_today_usd"])
                                 if st["daily_budget_usd"] > 0 else None)
        st["on_active_roster"] = name in ACTIVE_STRATEGIES
        st["retired_reason"] = RETIRED.get(name)
        try:
            lab = db.query_one(
                "SELECT COUNT(*) n FROM trades WHERE strategy=? AND mode='lab'", (name,))
            st["lab_trades"] = int(lab["n"] or 0) if lab else 0
        except Exception:
            st["lab_trades"] = 0
        # The state that needs acting on, named so the card can say it plainly.
        if st["on_active_roster"]:
            st["standing"] = "active"
        elif st["lab_trades"]:
            st["standing"] = "in the lab"
        else:
            st["standing"] = "off the roster — still in the book"
        out.append(st)
    return out
