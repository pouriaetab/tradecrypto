"""Which coins we are allowed to trade, and the daily movers screen.

IMPORTANT HONESTY NOTE
----------------------
`SEED_RH_SYMBOLS` below is an UNVERIFIED starting guess at Robinhood's crypto
list. Robinhood changes it, and it varies by state (crypto agent trading is
restricted in some states, including New York). Nothing in this file should be
treated as authoritative until `rh_confirmed = 1`, which only happens after the
Robinhood MCP has actually returned the symbol. Until then the UI shows the
symbol greyed with an "unconfirmed" badge and the engine will not trade it in
live mode.
"""
from __future__ import annotations

import json
import time

from app.core import db
from app.data.feeds import get_feed

# Seed guess only -- reconciled against the live feed, confirmed only by the MCP.
SEED_RH_SYMBOLS: list[str] = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "LTC", "BCH",
    "XLM", "ETC", "UNI", "AAVE", "COMP", "SHIB", "PEPE", "BONK", "WIF",
    "DOT", "NEAR", "APT", "ATOM", "ARB", "OP", "SUI", "SEI", "TIA", "INJ",
    "RENDER", "FET", "GRT", "SAND", "MANA", "CRV", "MKR", "LDO", "STX",
    "HBAR", "ALGO", "VET", "FIL", "ICP", "IMX", "WLD", "ONDO", "ENA", "JUP",
    "POL", "TRUMP", "PENGU", "TON",
]


def refresh_universe() -> dict:
    """Intersect the seed list with what the data feed can actually price."""
    feed = get_feed()
    products = feed.products()
    now = time.time()
    rows, missing = [], []
    for sym in SEED_RH_SYMBOLS:
        product = products.get(sym)
        if not product:
            missing.append(sym)
            continue
        rows.append((sym, product, feed.name, 0, 1, None,
                     "seed list; NOT yet confirmed against Robinhood", now))

    db.executemany(
        """INSERT INTO universe(symbol, feed_product, feed_source, rh_confirmed,
                                active, min_notional, note, updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
             feed_product=excluded.feed_product,
             feed_source=excluded.feed_source,
             active=excluded.active,
             updated_at=excluded.updated_at""",
        rows,
    )
    db.log_event("INFO", "universe",
                 f"refreshed: {len(rows)} priceable, {len(missing)} unavailable on {feed.name}",
                 {"missing": missing})
    return {
        "priceable": len(rows),
        "unavailable_on_feed": missing,
        "feed": feed.name,
        "rh_confirmed_count": db.query_one(
            "SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"],
        "warning": ("Symbols are NOT confirmed tradeable on Robinhood until the "
                    "agent MCP returns them. Live mode refuses unconfirmed symbols."),
    }


def active_universe(require_rh_confirmed: bool = False) -> list[dict]:
    sql = "SELECT * FROM universe WHERE active=1"
    if require_rh_confirmed:
        sql += " AND rh_confirmed=1"
    return db.query(sql + " ORDER BY symbol")


def mark_rh_confirmed(symbols: list[str]) -> int:
    """Called ONLY with symbols the Robinhood MCP actually returned."""
    now = time.time()
    db.executemany(
        "UPDATE universe SET rh_confirmed=1, note='confirmed by Robinhood MCP', updated_at=? WHERE symbol=?",
        [(now, s.upper()) for s in symbols],
    )
    return len(symbols)


def movers(limit: int = 25) -> list[dict]:
    """The 'biggest % movers' screen, with the caveat attached.

    the operator's observation is that Robinhood's own top-movers list is where the
    action is. That is true -- and it is also where the crowding, the widest
    spreads and the worst fills are. Every row therefore carries its own
    quoted spread so a mover that cannot be traded profitably is visible as
    such rather than merely exciting.

    This used to call Coinbase twice per coin, serially -- 75 coins, 150 HTTPS
    round trips, measured at 19.5 to 20.6 seconds. The frontend deadline is 20
    seconds and /movers was not on the slow-route allowlist, so the browser
    aborted EVERY request while the server dutifully finished it. The page was
    never empty because of the universe or the feed; it was empty because nobody
    was still listening. Worse, the retry and the 30-second poll overlapped, so
    each attempt made the next one slower.

    The 24-hour numbers are already in the bars table, refreshed every few
    minutes. Computing them there is one SQL query instead of 150 network calls,
    it cannot time out, and it works when the feed is down. Spread comes from the
    measured per-coin table rather than a live quote for the same reason.
    """
    import time as _time

    now = _time.time()
    since = now - 24 * 3600 - 120          # a little slack for bar alignment
    rows = {r["symbol"]: r for r in active_universe()}
    if not rows:
        return []
    place = ",".join("?" * len(rows))
    # One pass: the oldest and newest close in the window, plus high/low/volume.
    # 900s bars are the workhorse -- current for every coin, fine enough for a
    # 24h change. 3600s is the fallback for a coin that has no 15-minute data.
    agg: dict[str, dict] = {}
    for gran in (900, 3600):
        got = db.query(
            f"""SELECT symbol,
                       MIN(ts) AS t_first, MAX(ts) AS t_last,
                       MAX(high) AS hi, MIN(low) AS lo,
                       SUM(COALESCE(volume, 0)) AS vol, COUNT(*) AS n
                FROM bars
                WHERE granularity=? AND ts >= ? AND close IS NOT NULL
                  AND symbol IN ({place})
                GROUP BY symbol""", (gran, since, *rows))
        for r in got:
            if r["symbol"] in agg or (r["n"] or 0) < 4:
                continue
            agg[r["symbol"]] = {**dict(r), "gran": gran}
    if not agg:
        return []
    # First and last close per coin, fetched by the exact timestamps above.
    edges: dict[tuple, float] = {}
    for sym, a in agg.items():
        for key, ts_ in (("first", a["t_first"]), ("last", a["t_last"])):
            row = db.query_one(
                "SELECT close FROM bars WHERE symbol=? AND granularity=? AND ts=?",
                (sym, a["gran"], ts_))
            if row and row["close"] is not None:
                edges[(sym, key)] = float(row["close"])
    spreads = {r["symbol"]: r["spread_pct"] for r in db.query(
        "SELECT symbol, spread_pct FROM rh_spreads")} if db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rh_spreads'") else {}

    out = []
    for sym, a in agg.items():
        o_px, c_px = edges.get((sym, "first")), edges.get((sym, "last"))
        if not (o_px and c_px):
            continue
        row = rows[sym]
        sp = spreads.get(sym)
        out.append({
            "symbol": sym,
            "product": row["feed_product"],
            "last": c_px,
            "mid": c_px,
            "change_24h_pct": (c_px / o_px - 1.0) * 100.0,
            "high_24h": a["hi"],
            "low_24h": a["lo"],
            "range_24h_pct": ((a["hi"] - a["lo"]) / a["lo"] * 100.0) if a["lo"] else None,
            "volume_24h_base": a["vol"],
            "dollar_volume_24h": (a["vol"] * c_px) if c_px else None,
            "quoted_spread_bps": (float(sp) * 100.0) if sp is not None else None,
            "rh_confirmed": bool(row["rh_confirmed"]),
            "bars_used": a["n"],
            "resolution_s": a["gran"],
            "as_of": a["t_last"],
            "minutes_stale": round((now - a["t_last"]) / 60.0, 1),
        })
    out.sort(key=lambda r: abs(r["change_24h_pct"]), reverse=True)
    return out[:limit]


def adopt_from_robinhood() -> dict:
    """Grow the universe to EVERY coin Robinhood's API will trade and our feed can price.

    SEED_RH_SYMBOLS above was a hand-written guess, and it was wrong in the way
    that costs money: coins like AERO and SKL move hard, are tradable on
    Robinhood, and were never even watched. Every 'no opportunity found' result
    this project has produced was measured on the seed list, not on what
    Robinhood actually offers. This closes that gap.
    """
    from app.execution.rh_api import RobinhoodCrypto

    pairs = RobinhoodCrypto().trading_pairs()
    tradable: set[str] = set()
    for pair in pairs:
        sym = (pair.get("symbol") or "").upper()
        base = sym.split("-")[0]
        if base and sym.endswith("-USD") and pair.get("is_api_tradable"):
            tradable.add(base)

    feed = get_feed()
    products = feed.products()
    now = time.time()
    have = {r["symbol"] for r in db.query("SELECT symbol FROM universe")}

    rows, added, no_price = [], [], []
    for sym in sorted(tradable - have):
        product = products.get(sym)
        if not product:
            no_price.append(sym)
            continue
        rows.append((sym, product, feed.name, 1, 1, None,
                     "adopted: Robinhood says is_api_tradable", now))
        added.append(sym)

    if rows:
        db.executemany(
            """INSERT INTO universe(symbol, feed_product, feed_source, rh_confirmed,
                                    active, min_notional, note, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 rh_confirmed=excluded.rh_confirmed,
                 active=excluded.active,
                 updated_at=excluded.updated_at""",
            rows,
        )
    # Report the WHOLE picture, not just the delta.
    #
    # "adopted 0 coins" was logged every boot and read as "we have everything".
    # It does not say how many pairs Robinhood returned, how many of those the
    # API will actually trade, or how many were dropped because our price feed
    # cannot quote them -- and that last number is the one that matters when the
    # Robinhood website shows far more coins than this desk tracks.
    db.log_event(
        "INFO", "universe",
        f"Robinhood returned {len(pairs)} pairs; {len(tradable)} are USD and "
        f"is_api_tradable; {len(added)} newly adopted; "
        f"{len(no_price)} dropped because the price feed cannot quote them"
        + (f" ({', '.join(sorted(no_price)[:12])})" if no_price else ""),
        {"pairs_returned": len(pairs), "api_tradable": len(tradable),
         "added": added, "no_feed_price": no_price})
    out = {
        "pairs_returned_by_robinhood": len(pairs),
        "api_tradable_on_robinhood": len(tradable),
        "already_tracked": len(have),
        "added": added,
        "added_count": len(added),
        "tradable_but_feed_cannot_price": no_price,
        "universe_now": len(have) + len(added),
        "checked_at": now,
        "next": ("Backfill history for the new coins before trusting any signal on "
                 "them -- they have no bars yet."),
    }
    # Persist it. These counts existed only in this return value and in a log
    # line, so the four tiles on the Universe page could only ever be filled by
    # pressing a button in that same browser session -- and the button called a
    # DIFFERENT endpoint, whose response uses different key names, so they showed
    # two dashes and two zeros forever. Written down, they render on page load.
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_ts=excluded.updated_ts",
        ("robinhood_list_counts", json.dumps(out), now))
    return out


def robinhood_list_counts() -> dict:
    """The last Robinhood list check, as recorded by adopt_from_robinhood()."""
    row = db.query_one("SELECT value FROM app_state WHERE key=?", ("robinhood_list_counts",))
    if not row or not row["value"]:
        return {"never_checked": True}
    try:
        return json.loads(row["value"])
    except Exception:
        return {"never_checked": True}
