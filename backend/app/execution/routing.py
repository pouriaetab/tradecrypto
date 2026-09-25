"""Which routing we are priced under, and what that actually costs.

Read out of docs.robinhood.com/crypto/trading on 2026-09-15, which this project
had never opened. The documentation is explicit, and the two paths are not the
same product:

  v1  /api/v1/crypto/marketdata/best_bid_ask/
      "the best available price across our partner MARKET MAKERS ... The bid and
      ask prices INCLUDE A SPREAD. The buy spread is the percent difference
      between the ask and the mid price."

  v2  /api/v2/crypto/marketdata/best_bid_ask/
      "the best available price across our partner EXCHANGES. This price does not
      take into account the order size OR FEE."

So under v1 the cost is baked into the price (~0.95% a side, which is what this
desk measured on 33 coins). Under v2 the price is the exchange's and the cost is
a separate FEE set by trailing 30-day volume — 0.95% at the bottom tier, 0.25%
above $50K, 0.15% above $250K.

The consequence for every result in this project: the 1.9182% round trip is not
a property of Robinhood. It is a property of the BOTTOM TIER. Nothing here had
modelled that, because nobody had read the reference.

Two further facts from the same source, both load-bearing:

  * Order types are market, limit, stop_loss and stop_limit, and limit orders
    take `limit_price` and `time_in_force` (gtc). A limit order is a maker order
    on exchange routing.
  * v2 orders are charged the TAKER rate until maker/taker finishes rolling out,
    so the maker column is not reachable yet and must not be planned against.
    v1 orders do not count toward volume at all.
"""
from __future__ import annotations

import time

from app.core import db
from app.execution.venue_fees import FEE_TIERS, fee_for_volume

MARKET_MAKER_SPREAD_PCT = 0.95          # measured on 33 coins; matches their docs


def exact_round_trip_pct(per_side_pct: float) -> float:
    """(1+s)/(1-s) - 1, the break-even move. Never 2s."""
    s = float(per_side_pct) / 100.0
    if not (0 <= s < 0.5):
        return 0.0
    return ((1 + s) / (1 - s) - 1) * 100.0


def thirty_day_volume_usd(mode: str | None = None) -> float:
    """Our own trailing 30-day notional, both sides, from filled orders.

    Only exchange-routed orders count toward a Robinhood fee tier. We cannot see
    routing per order, so this is what our volume WOULD be if every order were
    exchange routed -- an upper bound, and labelled as one.
    """
    since = time.time() - 30 * 86400
    q = ("SELECT COALESCE(SUM(notional_usd),0) v FROM orders "
         "WHERE status='filled' AND ts_filled >= ?")
    args: tuple = (since,)
    if mode:
        q += " AND mode=?"
        args = (since, mode)
    row = db.query_one(q, args)
    return float(row["v"] or 0.0)


def tier_for(volume_usd: float) -> dict:
    tier = FEE_TIERS[0]
    nxt = None
    for i, t in enumerate(FEE_TIERS):
        if volume_usd >= t["min_volume_usd"]:
            tier = t
            nxt = FEE_TIERS[i + 1] if i + 1 < len(FEE_TIERS) else None
    return {
        "min_volume_usd": tier["min_volume_usd"],
        "taker_pct": tier["taker_pct"],
        "maker_pct": tier["maker_pct"],
        "round_trip_taker_pct": exact_round_trip_pct(tier["taker_pct"]),
        "round_trip_maker_pct": exact_round_trip_pct(tier["maker_pct"]),
        "next_tier_at_usd": nxt["min_volume_usd"] if nxt else None,
        "next_tier_taker_pct": nxt["taker_pct"] if nxt else None,
        "next_tier_round_trip_pct": exact_round_trip_pct(nxt["taker_pct"]) if nxt else None,
        "volume_to_next_usd": (nxt["min_volume_usd"] - volume_usd) if nxt else None,
    }


def picture(mode: str = "paper") -> dict:
    """Everything the desk should know about what a round trip costs, and why."""
    vol = thirty_day_volume_usd(mode)
    tier = tier_for(vol)
    mm = exact_round_trip_pct(MARKET_MAKER_SPREAD_PCT)

    # What each active strategy's measured held-out gross edge nets under each.
    # These are the numbers this project actually measured; kept here so the
    # comparison is concrete rather than abstract.
    edges = [
        ("pump_ride", 1.56), ("volume_build", 0.98), ("oversold_turn", 2.77),
        ("morning_dip", 0.21), ("day_climb", -0.17),
    ]
    levels = [("market maker (default today)", mm),
              (f"exchange, this tier ({tier['taker_pct']:.3f}% taker)",
               tier["round_trip_taker_pct"])]
    if tier["next_tier_round_trip_pct"] is not None:
        levels.append((f"exchange, next tier ({tier['next_tier_taker_pct']:.3f}% taker)",
                       tier["next_tier_round_trip_pct"]))
    for t in FEE_TIERS:
        if t["min_volume_usd"] == 50_000:
            levels.append(("exchange, $50K tier (0.250% taker)",
                           exact_round_trip_pct(t["taker_pct"])))

    return {
        "as_of": time.time(),
        "mode": mode,
        "routing_in_use": "market_maker",
        "routing_note": (
            "Market Maker Routing is Robinhood's default and is what every number in "
            "this app has assumed. Exchange Routing prices from partner exchanges and "
            "charges a volume-tiered fee instead of a spread. Nothing here has ever "
            "placed an exchange-routed order, so the tier below is what our volume "
            "WOULD qualify for, not what we are paying."),
        "market_maker_spread_pct": MARKET_MAKER_SPREAD_PCT,
        "market_maker_round_trip_pct": mm,
        "trailing_30d_volume_usd": round(vol, 2),
        "tier": tier,
        "maker_reachable": False,
        "maker_note": (
            "A limit order is a maker order on exchange routing, but Robinhood charges "
            "v2 API orders the TAKER rate until maker/taker finishes rolling out. Plan "
            "against the taker column."),
        "cost_levels": [{"label": k, "round_trip_pct": round(v, 4)} for k, v in levels],
        "strategy_net_by_level": [
            {"strategy": n, "gross_pct": g,
             "net": [{"label": k, "net_pct": round(g - v, 4)} for k, v in levels]}
            for n, g in edges
        ],
    }


def estimated_cost_live(symbol: str, notional_usd: float) -> dict:
    """Ask Robinhood what a real order of this size would actually cost.

    v2 `estimated_price` returns "the estimated total cost or credit ... and fee",
    which is the one number this project has been assuming rather than measuring.
    Read-only: it places nothing.
    """
    out: dict = {"symbol": symbol, "notional_usd": notional_usd,
                 "error": "no broker integration in this build — the order-placing and account code was removed when this repository was published"}
    return out
    try:
        c = None
        book = c.best_bid_ask(symbol, v2=True)
        rows = book.get("results") if isinstance(book, dict) else book
        row = (rows or [{}])[0]
        bid = row.get("bid_price") or row.get("bid")
        ask = row.get("ask_price") or row.get("ask")
        mid = ((float(bid) + float(ask)) / 2.0) if bid and ask else None
        out["v2_book"] = {"bid": bid, "ask": ask, "mid": mid}
        if mid:
            qty = max(notional_usd / mid, 0.0)
            est = c.estimated_price(symbol, "both", f"{qty:.8f}", v2=True)
            out["v2_estimated"] = est.get("results") if isinstance(est, dict) else est
            out["quantity_used"] = round(qty, 8)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
