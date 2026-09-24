"""What each venue costs, and what that does to a strategy that has an edge.

The finding this exists to serve
--------------------------------
research/edge_audit.py measured, on this operator's own data with a three-way
time split: the gross edge available to technical indicators over a 6-12 hour
hold is roughly 1 to 12 basis points per trade. Robinhood's round trip is 192.
The model is not the binding constraint; the venue is.

So the question stops being "can we predict better" and becomes "at what cost is
this arithmetic survivable". This module answers that with a number per venue
rather than an opinion.

On the numbers below
--------------------
Fee schedules change, differ by country, and are reported inconsistently even by
comparison sites — Kraken's own page and third-party guides disagreed by a factor
of three at the entry tier while this was written. So every figure here is
EDITABLE and carries its source and date, exactly like the Robinhood spread. A
venue you have not verified against your own account statement is marked
unverified and says so on screen.

Verify before acting. The whole point of this page is that a decision this large
should not rest on a number somebody read on a blog.

Maker versus taker
------------------
A taker order crosses the spread now. A maker order rests and waits, and pays
much less — often half. That matters more than any indicator in this project:
at Coinbase Advanced's entry tier the difference between the two is 40 bps a
round trip, which is larger than the entire measured edge, several times over.
The cost of a maker order is that it may not fill, which for a strategy holding
6-12 hours is a far smaller problem than for one holding 5 minutes.
"""
from __future__ import annotations

import time

from app.core import db

# Seeds. Each is a claim with a source, not a fact. `verified` becomes true only
# when the operator confirms it against their own account.
SEED_VENUES = [
    {
        "key": "robinhood", "name": "Robinhood Crypto", "model": "spread",
        "maker_bps": None, "taker_bps": None, "spread_pct": 0.95,
        "verified": True,
        "source": "Order ticket, DOGE, 2026-09-06: mid 0.090569, buy 0.091485, bid 0.089704",
        "note": "No commission; the whole cost is the spread, stated on the ticket. "
                "This is Market Maker Routing, Robinhood's DEFAULT. Correction "
                "(2026-09-14, from their own order-routing page): it is NOT the only "
                "option. Exchange Routing sends orders to EDX Markets and Bitstamp "
                "instead, charges a commission by 30-day volume tier rather than a "
                "spread, and there a LIMIT order pays the maker fee. The claim that "
                "every Robinhood order is a market order at their quote was wrong.",
        "supports_limit_orders": False,
    },
    {
        "key": "robinhood_v2_taker", "name": "Robinhood API v2 (exchange routing, taker)",
        "model": "fee", "maker_bps": 50.0, "taker_bps": 95.0, "spread_pct": None,
        "verified": True,
        "source": "Robinhood fee schedule PDF, read 2026-09-07: $0-10K tier is "
                  "0.95% taker / 0.50% maker. Falls to 0.25%/0.125% above $50K "
                  "of trailing 30-day volume.",
        "note": "The v2 API charges the TAKER rate until Robinhood finishes rolling "
                "out maker/taker, so 0.50% maker is not reachable through the API "
                "yet. The tiers fall steeply with volume: $50K/month takes the round "
                "trip from 190 bps to 50 bps.",
        "supports_limit_orders": True,
    },
    {
        "key": "coinbase_advanced", "name": "Coinbase Advanced Trade", "model": "fee",
        "maker_bps": 40.0, "taker_bps": 60.0, "spread_pct": None,
        "verified": False,
        "source": "datawallet.com and traderssecondbrain.com, both read 2026-09-06, "
                  "entry tier (under $10k 30-day volume). NOT yet checked against a "
                  "real Coinbase statement.",
        "note": "Fee on top of the real order-book price, so the effective spread is "
                "the book's own — typically a few bps on majors, versus Robinhood's "
                "fixed 95.",
        "supports_limit_orders": True,
    },
    {
        "key": "kraken_pro", "name": "Kraken Pro", "model": "fee",
        "maker_bps": 40.0, "taker_bps": 80.0, "spread_pct": None,
        "verified": True,
        "source": "kraken.com/features/fee-schedule, read again 2026-09-21: Kraken "
                  "Pro spot tier 1 ($0+ 30-day volume) is 0.40% maker / 0.80% taker. "
                  "The seeded 0.26/0.40 was a split-the-difference against a "
                  "third-party page (traderssecondbrain) that was simply wrong; "
                  "Kraken's own schedule is the only source used now. The 1% / 1.5% "
                  "figures elsewhere are the simple Kraken APP, not Pro.",
        "note": "A FEE on top of the real order-book price, so the taker also pays "
                "the book's spread. Measured 2026-09-21 on the 37 coins this desk "
                "trades: median half-spread 0.034%, worst 0.104% (FLOKI) -- against "
                "Robinhood's fixed 0.95% a side. So taker ~1.67% round trip is only "
                "a little cheaper than Robinhood's 1.90%; the real prize is maker at "
                "0.80% round trip, IF a resting order fills, which for a momentum "
                "entry is exactly when it may not.",
        "supports_limit_orders": True,
    },
    {
        "key": "coinbase_simple", "name": "Coinbase (simple / retail app)", "model": "fee",
        "maker_bps": None, "taker_bps": 150.0, "spread_pct": 0.5,
        "verified": False,
        "source": "datawallet.com read 2026-09-06: 'a visible fee alongside a spread "
                  "embedded in the quoted price', up to 1.875%. Seeded pessimistically.",
        "note": "Listed to show what the retail path costs. It is worse than Robinhood.",
        "supports_limit_orders": False,
    },
]


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS venues (
        key TEXT PRIMARY KEY, name TEXT NOT NULL, model TEXT NOT NULL,
        maker_bps REAL, taker_bps REAL, spread_pct REAL,
        verified INTEGER NOT NULL DEFAULT 0, source TEXT, note TEXT,
        supports_limit_orders INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL
    )""")
    # The seed used to run ONLY into an empty table, and then only with INSERT OR
    # IGNORE. That made a seeded guess permanent: kraken_pro sat at 26/40 bps for
    # a fortnight -- a split-the-difference between Kraken's own schedule and a
    # third-party page -- with verified=0 and a source line literally saying "must
    # be verified before it is trusted". When it WAS verified (2026-09-21:
    # Kraken's own page, 40/80 at tier 1) the corrected seed could not reach the
    # database, because a row was already there.
    #
    # So: a row the database marks verified=0 is a PLACEHOLDER and a verified seed
    # replaces it. A row already marked verified is left alone -- it may have been
    # corrected by hand from a real statement, which outranks anything shipped in
    # source code.
    for v in SEED_VENUES:
        cur = db.query_one("SELECT verified FROM venues WHERE key=?", (v["key"],))
        if cur is not None and (int(cur["verified"] or 0) == 1 or not v["verified"]):
            continue
        db.execute(
            """INSERT INTO venues(key,name,model,maker_bps,taker_bps,
                   spread_pct,verified,source,note,supports_limit_orders,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 name=excluded.name, model=excluded.model, maker_bps=excluded.maker_bps,
                 taker_bps=excluded.taker_bps, spread_pct=excluded.spread_pct,
                 verified=excluded.verified, source=excluded.source, note=excluded.note,
                 supports_limit_orders=excluded.supports_limit_orders,
                 updated_at=excluded.updated_at""",
            (v["key"], v["name"], v["model"], v["maker_bps"], v["taker_bps"],
             v["spread_pct"], int(v["verified"]), v["source"], v["note"],
             int(v["supports_limit_orders"]), time.time()))


def round_trip_bps(v: dict, style: str = "taker") -> float | None:
    """Cost of a full in-and-out, in basis points.

    Spread venues use the exact break-even (1+s)/(1-s)-1 rather than 2s, because
    the exit loss is taken on a larger base. Fee venues pay the fee twice, plus
    the book's own spread, which for majors is small enough that it is shown
    separately rather than guessed at here.
    """
    if v["model"] == "spread":
        s = (v["spread_pct"] or 0) / 100.0
        rt = ((1 + s) / (1 - s) - 1) * 1e4 if 0 < s < 0.5 else 0.0
        extra = (v["taker_bps"] or 0.0) * 2
        return rt + extra
    side = v["maker_bps"] if style == "maker" else v["taker_bps"]
    if side is None:
        return None
    rt = 2 * side
    if v["spread_pct"]:                       # a fee venue that also has a spread
        s = v["spread_pct"] / 100.0
        rt += ((1 + s) / (1 - s) - 1) * 1e4
    return rt


def venues() -> list[dict]:
    ensure_schema()
    out = []
    for r in db.query("SELECT * FROM venues ORDER BY key"):
        d = dict(r)
        d["verified"] = bool(d["verified"])
        d["supports_limit_orders"] = bool(d["supports_limit_orders"])
        d["round_trip_taker_bps"] = round_trip_bps(d, "taker")
        d["round_trip_maker_bps"] = (round_trip_bps(d, "maker")
                                     if d["supports_limit_orders"] else None)
        out.append(d)
    return out


def set_venue(key: str, **fields) -> dict:
    ensure_schema()
    allowed = {"name", "maker_bps", "taker_bps", "spread_pct", "verified",
               "source", "note", "supports_limit_orders", "model"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            params.append(int(v) if k in ("verified", "supports_limit_orders") else v)
    if not sets:
        return {"error": "nothing to update"}
    params += [time.time(), key]
    n = db.execute(f"UPDATE venues SET {', '.join(sets)}, updated_at=? WHERE key=?", params)
    if not n:
        return {"error": f"no venue {key!r}"}
    db.log_event("INFO", "cost", f"venue {key} updated: {sorted(fields)}")
    return next(v for v in venues() if v["key"] == key)


# What edge.audit measured, so the comparison uses this operator's real numbers
# rather than an illustrative one.
MEASURED = {
    "unconditional_6h_bps": 11.8,
    "unconditional_12h_bps": 23.3,
    "model_top_decile_bps": 1.4,
    "source": "research/edge_audit.py, 794,361 fifteen-minute bars over 24 coins, "
              "three-way time split, 2026-09-06",
}


def compare(gross_edge_bps: float | None = None, trades_per_day: float = 2.0,
            equity_usd: float = 500.0) -> dict:
    """The decision table: at each venue, does the measured edge survive?"""
    edge = MEASURED["unconditional_12h_bps"] if gross_edge_bps is None else float(gross_edge_bps)
    rows = []
    for v in venues():
        for style in ("taker", "maker"):
            rt = v["round_trip_taker_bps"] if style == "taker" else v["round_trip_maker_bps"]
            if rt is None:
                continue
            net = edge - rt
            rows.append({
                "venue": v["name"], "key": v["key"], "style": style,
                "round_trip_bps": rt,
                "net_bps_per_trade": net,
                "profitable": net > 0,
                "edge_multiple_needed": (rt / edge) if edge > 0 else None,
                "daily_usd": equity_usd * (net / 1e4) * trades_per_day,
                "verified": v["verified"],
            })
    rows.sort(key=lambda r: r["round_trip_bps"])
    best = rows[0] if rows else None
    return {
        "gross_edge_bps": edge,
        "edge_source": MEASURED["source"],
        "measured": MEASURED,
        "assumptions": {"trades_per_day": trades_per_day, "equity_usd": equity_usd},
        "rows": rows,
        "cheapest": best,
        "verdict": _verdict(rows, edge),
    }


def _verdict(rows: list[dict], edge: float) -> str:
    if not rows:
        return "no venues configured"
    winners = [r for r in rows if r["profitable"]]
    best = rows[0]
    if winners:
        w = winners[0]
        return (f"{w['venue']} as a {w['style']} is the only arithmetic that works: "
                f"{w['round_trip_bps']:.0f} bps round trip against a {edge:.1f} bps "
                f"edge leaves {w['net_bps_per_trade']:+.1f} bps a trade. That is thin, "
                f"and it assumes the measured edge survives contact with a new venue.")
    return (f"No venue works at the measured edge. The cheapest option is "
            f"{best['venue']} as a {best['style']} at {best['round_trip_bps']:.0f} bps, "
            f"which is still {best['edge_multiple_needed']:.0f}x the {edge:.1f} bps of "
            f"gross edge the indicators actually produced. Moving venue narrows the gap "
            f"but does not close it — the strategy needs a bigger edge, not just a "
            f"cheaper toll.")
