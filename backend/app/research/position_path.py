"""How far up and how far down each open position has been, net of both sides.

WHAT THE OPERATOR ASKED FOR (2026-09-22)
----------------------------------------
"for all the current open positions ... show the highest and lowest point that
up to this point the profit and loss got to, meaning always after factoring in
the both side buy and sell, what was the highest and lowest amount we had and
give me the time after we got in when this happened ... and also another column
for how much time it was passed since we go in".

WHY THIS IS NOT THE SAME AS THE CURRENT P&L
-------------------------------------------
The Risk tab shows what a position is worth now. It cannot show that WLD was
$4 up two hours ago and is $1 down now -- and that difference is the whole
argument about exits. A rule that would have banked the peak and a rule that
holds to a target are indistinguishable on a screen that only reports "now".

HOW THE NUMBER IS BUILT, AND WHAT IT COSTS
------------------------------------------
Exactly the arithmetic `engine._mark_equity` uses, applied at every bar since
entry instead of only at the last one:

    value at a price p  =  (p * (1 - spread_per_side) - avg_px) * qty

`avg_px` is the fill, which already carries the BUY side's spread; multiplying
by (1 - spread) charges the SELL side. So both sides are in, at Robinhood's own
measured per-coin spread -- the same figure the book marks against, never a
guess.

The peak is taken from each bar's HIGH and the trough from its LOW, because
that is what "the highest amount we had" means. A close-only walk would quietly
report a smaller range than really happened.

WHAT IT CANNOT TELL YOU, STATED
-------------------------------
The timestamp is the BAR the extreme fell in, not the minute. At 15-minute bars
the peak is located to within that quarter hour and no better, and the output
says so in `granularity_s` rather than implying a precision it does not have.
Bars before the entry are excluded, so a high printed minutes before the fill is
never credited to the trade.
"""
from __future__ import annotations

import time

from app.core import db

# Finest first: use the best resolution this coin actually has.
GRANULARITIES = (900, 3600, 21600, 86400)


def _spread_frac(symbol: str) -> tuple[float, bool]:
    """Robinhood's measured per-side spread for this coin, and whether it is real."""
    try:
        from app.execution import rh_spread
        s = rh_spread.get(symbol)
        return float(s.get("spread_pct") or 0.0) / 100.0, bool(s.get("is_measured"))
    except Exception:
        return 0.0095, False


def _bars_since(symbol: str, since: float) -> tuple[list[dict], int]:
    """Finest bars this coin has since `since`. Returns ([], 0) rather than
    raising: no price history is a reason to say "cannot be reconstructed" for
    ONE column, never a reason to lose the position's size and age as well.
    (A trimmed ledger snapshot has no bars table at all, which is how this was
    found -- the whole row came back empty.)"""
    for g in GRANULARITIES:
        try:
            rows = db.query(
                "SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=? "
                "AND ts >= ? ORDER BY ts", (symbol, g, int(since)))
        except Exception:
            return [], 0
        if len(rows) >= 2:
            return [dict(r) for r in rows], g
    return [], 0


def for_position(p: dict) -> dict:
    """Peak, trough and now for one open position. Never raises."""
    sym = p["symbol"]
    qty = float(p["qty"] or 0.0)
    entry = float(p["avg_px"] or 0.0)
    opened = float(p["opened_ts"] or 0.0)
    side, measured = _spread_frac(sym)
    out = {
        "symbol": sym, "strategy": p.get("strategy"),
        "qty": qty, "entry_px": entry, "opened_ts": opened,
        "cost_basis_usd": qty * entry,
        "age_s": max(0.0, time.time() - opened),
        "spread_pct_per_side": side * 100.0,
        "spread_is_measured": measured,
        "peak_usd": None, "peak_ts": None, "peak_after_s": None,
        "trough_usd": None, "trough_ts": None, "trough_after_s": None,
        "now_usd": None, "granularity_s": 0, "bars": 0, "why": None,
    }
    if qty == 0 or entry <= 0:
        out["why"] = "no position"
        return out

    def value(px: float) -> float:
        return (px * (1.0 - side) - entry) * qty

    bars, g = _bars_since(sym, opened)
    if not bars:
        out["why"] = ("no stored bars since this position opened — the peak and "
                      "trough cannot be reconstructed, only the current mark")
    else:
        out["granularity_s"] = g
        out["bars"] = len(bars)
        hi = max(bars, key=lambda b: (b["high"] if b["high"] is not None else -1e18))
        lo = min(bars, key=lambda b: (b["low"] if b["low"] is not None else 1e18))
        if hi["high"] is not None:
            out["peak_usd"] = value(float(hi["high"]))
            out["peak_ts"] = float(hi["ts"])
            out["peak_after_s"] = max(0.0, float(hi["ts"]) - opened)
        if lo["low"] is not None:
            out["trough_usd"] = value(float(lo["low"]))
            out["trough_ts"] = float(lo["ts"])
            out["trough_after_s"] = max(0.0, float(lo["ts"]) - opened)

    # "Now" is the live mid if there is one, otherwise the newest bar close --
    # the same fallback order the engine marks the book with.
    px = None
    try:
        q = db.query_one("SELECT mid FROM quotes WHERE symbol=? ORDER BY ts DESC, id DESC LIMIT 1",
                         (sym,))
        if q and q["mid"]:
            px = float(q["mid"])
    except Exception:
        pass
    if px is None and bars:
        try:
            px = float(bars[-1]["close"] or 0.0) or None
        except Exception:
            px = None
    if px:
        out["now_usd"] = value(px)
        out["now_px"] = px
    # How much of the best it reached is still on the table. This is the number
    # the exit argument actually turns on.
    if out["peak_usd"] is not None and out["now_usd"] is not None:
        out["given_back_usd"] = out["peak_usd"] - out["now_usd"]
    return out


def open_paths(mode: str = "paper") -> dict:
    """Every open position, with how far up and down it has been."""
    from app.risk import guards
    rows = []
    for p in guards.open_positions(mode):
        try:
            rows.append(for_position(p))
        except Exception as exc:
            rows.append({"symbol": p.get("symbol"), "strategy": p.get("strategy"),
                         "why": f"{type(exc).__name__}: {exc}"})
    rows.sort(key=lambda r: -(r.get("age_s") or 0))
    have = [r for r in rows if r.get("peak_usd") is not None]
    return {
        "mode": mode,
        "positions": rows,
        "totals": {
            "n": len(rows),
            "reconstructed": len(have),
            "peak_usd": sum(r["peak_usd"] for r in have) if have else None,
            "trough_usd": sum(r["trough_usd"] for r in have if r.get("trough_usd") is not None) if have else None,
            "now_usd": sum(r["now_usd"] for r in rows if r.get("now_usd") is not None),
            "given_back_usd": sum(r["given_back_usd"] for r in rows if r.get("given_back_usd") is not None),
        },
        "note": ("Both sides are charged: the entry price already carries the buy "
                 "spread, and the mark is taken at what the coin would SELL for. "
                 "Peak uses each bar's high and trough its low, so the times are "
                 "located to the bar, not the minute."),
        "generated_at": time.time(),
    }
