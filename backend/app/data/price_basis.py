"""Does our price actually match Robinhood's?

The question that prompted this
--------------------------------
Every number in this system is computed from Coinbase candles. Every decision is
then executed — eventually — at Robinhood's price. If those two disagree, the
whole apparatus is measuring one market and trading another, and no amount of
statistical care downstream would rescue it.

Until this module, that had never been checked. It had been *assumed*, which is
the same class of mistake as assuming the memory watchdog was running.

The one measurement that existed
--------------------------------
A DOGE order ticket on 2026-09-06 showed a mid of 0.090569. Our Coinbase 15-minute
close at 16:30 local that day was 0.090540 — a basis of 3.2 bps. That is
reassuring and it is also a single anecdote, taken at a moment nobody chose
carefully. One point is not a distribution.

So this records them properly. You read a mid off a Robinhood ticket, type it in
with the coin, and it is compared against our feed at that instant. Enough of
those and "our data matches Robinhood" stops being a belief.

How to read the basis
---------------------
    basis_bps = (feed_mid - rh_mid) / rh_mid * 10000

Near zero means the two venues agree and the research is measuring the right
market. A consistent non-zero average means a systematic offset, which is worth
knowing but is not itself a problem — it cancels on a round trip, since we buy
and sell at the same venue. What WOULD be a problem is a large or erratic basis,
because that means the entry price our signals assume is not the price we get,
and the difference is noise we cannot model.

A caution about timing. Prices move. A basis measured a few minutes off the
ticket is mostly measuring the clock, not the venue: DOGE moved 116 bps over the
two hours around that screenshot. Record the mid promptly, and treat a large
basis on a stale observation as a timing artefact until proven otherwise.
"""
from __future__ import annotations

import time

from app.core import db

TOLERANCE_BPS = 25.0        # beyond this, look at the timestamp before the venue


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS price_basis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, symbol TEXT NOT NULL,
        rh_mid REAL NOT NULL, feed_mid REAL, feed_source TEXT,
        feed_ts REAL, lag_s REAL, basis_bps REAL, note TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_basis_ts ON price_basis(ts DESC)")


def _nearest_feed_price(symbol: str, at: float) -> tuple[float | None, float | None, str]:
    """Our own view of the price closest in time to the observation."""
    q = db.query_one(
        """SELECT ts, (bid+ask)/2.0 AS mid, source FROM quotes
           WHERE symbol=? AND bid IS NOT NULL AND ask IS NOT NULL
             AND source != 'robinhood'
           ORDER BY ABS(ts - ?) LIMIT 1""", (symbol.upper(), at))
    if q and q["mid"]:
        return float(q["mid"]), float(q["ts"]), (q["source"] or "quote")
    b = db.query_one(
        """SELECT ts, close, source FROM bars
           WHERE symbol=? AND granularity=60 ORDER BY ABS(ts - ?) LIMIT 1""",
        (symbol.upper(), at))
    if not b:
        b = db.query_one(
            """SELECT ts, close, source FROM bars
               WHERE symbol=? AND granularity=900 ORDER BY ABS(ts - ?) LIMIT 1""",
            (symbol.upper(), at))
    if b:
        return float(b["close"]), float(b["ts"]), (b["source"] or "bar")
    return None, None, "none"


def record(symbol: str, rh_mid: float, at: float | None = None,
           note: str | None = None) -> dict:
    ensure_schema()
    at = float(at) if at else time.time()
    rh_mid = float(rh_mid)
    if rh_mid <= 0:
        return {"error": "the Robinhood mid must be a positive price"}
    feed, fts, src = _nearest_feed_price(symbol, at)
    basis = ((feed - rh_mid) / rh_mid * 1e4) if feed else None
    lag = abs(fts - at) if fts else None
    db.execute(
        """INSERT INTO price_basis(ts,symbol,rh_mid,feed_mid,feed_source,feed_ts,
                                   lag_s,basis_bps,note) VALUES (?,?,?,?,?,?,?,?,?)""",
        (at, symbol.upper(), rh_mid, feed, src, fts, lag, basis, note))
    return {
        "symbol": symbol.upper(), "rh_mid": rh_mid, "feed_mid": feed,
        "feed_source": src, "lag_s": lag, "basis_bps": basis,
        "reading": _reading(basis, lag),
    }


def _reading(basis: float | None, lag: float | None) -> str:
    if basis is None:
        return "no price of our own near that time — nothing to compare against"
    if lag is not None and lag > 900:
        return (f"our nearest price is {lag/60:.0f} minutes away, so this mostly "
                f"measures the clock rather than the venue")
    if abs(basis) <= TOLERANCE_BPS:
        return (f"{basis:+.1f} bps — our feed and Robinhood agree at this instant; "
                f"the research is measuring the market we would trade")
    return (f"{basis:+.1f} bps is wider than the {TOLERANCE_BPS:.0f} bps tolerance. "
            f"Check the observation time first; a venue really being this far apart "
            f"would matter.")


def report(days: float = 90.0) -> dict:
    ensure_schema()
    rows = db.query(
        """SELECT * FROM price_basis WHERE ts >= ? ORDER BY ts DESC LIMIT 500""",
        (time.time() - days * 86400,))
    usable = [r for r in rows if r["basis_bps"] is not None
              and (r["lag_s"] is None or r["lag_s"] <= 900)]
    vals = sorted(abs(r["basis_bps"]) for r in usable)
    signed = sorted(r["basis_bps"] for r in usable)

    def med(xs):
        return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else
                                    (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)

    by_symbol: dict[str, list] = {}
    for r in usable:
        by_symbol.setdefault(r["symbol"], []).append(r["basis_bps"])

    n = len(usable)
    return {
        "rows": rows,
        "n_usable": n,
        "median_abs_bps": med(vals),
        "median_signed_bps": med(signed),
        "worst_abs_bps": vals[-1] if vals else None,
        "tolerance_bps": TOLERANCE_BPS,
        "by_symbol": [{"symbol": k, "n": len(v), "median_bps": med(sorted(v))}
                      for k, v in sorted(by_symbol.items())],
        "verdict": (
            "No observations yet. Open a coin on Robinhood, read the mid off the "
            "order ticket, and record it here. Until then, 'our data matches "
            "Robinhood' is an assumption, not a measurement."
            if n == 0 else
            f"{n} observation(s). Median absolute basis {med(vals):.1f} bps"
            + (f", median signed {med(signed):+.1f} bps. "
               + ("The two venues agree closely enough that the research is measuring "
                  "the market we would trade."
                  if med(vals) <= TOLERANCE_BPS else
                  "This is wider than tolerance — check the observation times before "
                  "concluding the venues really differ.")
               if med(vals) is not None else "")),
        "how": ("On Robinhood, open the coin and press Buy or Sell. The ticket shows "
                "'Mid price'. Type that number here with the coin, promptly — a mid "
                "recorded ten minutes late measures the clock, not the venue."),
    }
