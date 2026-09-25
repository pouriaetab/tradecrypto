"""What a round trip costs, and why that is the whole story.

This is the single most important finding in the project, so it lives in its own
module rather than buried inside a broker client: the cost of trading, not the
quality of the signal, is what decides the outcome.

Source: Robinhood Crypto's published fee schedule (document dated 2026-06-22),
read directly from the PDF rather than from a summary.

THE PART THAT MATTERS. The tiers below apply only to EXCHANGE ROUTING. The
default is Market Maker Routing, which charges no commission at all and takes an
embedded spread instead: "for every $100 of notional crypto order volume
executed through market maker routing, Robinhood Crypto receives $0.95."

That is 0.95% per side, so about 1.90% for a round trip — and it is invisible,
because no line item ever says "fee". A backtest that models commission and
ignores the spread will report a profit that cannot exist.

For scale: the coins in this universe move 2-3% on a typical day. A strategy has
to be right by more than an entire day's normal move before it clears its own
costs. Every negative result in this repository follows from that one number.

No network access, no credentials, no account. Just the published schedule.
"""
from __future__ import annotations

# Tier is set by trailing 30-day volume of EXCHANGE-ROUTED orders only, and is
# assigned at the moment the order is placed.
FEE_TIERS: list[dict[str, float]] = [
    {"min_volume_usd": 0,          "taker_pct": 0.95,  "maker_pct": 0.50},
    {"min_volume_usd": 10_000,     "taker_pct": 0.75,  "maker_pct": 0.35},
    {"min_volume_usd": 50_000,     "taker_pct": 0.25,  "maker_pct": 0.125},
    {"min_volume_usd": 250_000,    "taker_pct": 0.15,  "maker_pct": 0.075},
    {"min_volume_usd": 500_000,    "taker_pct": 0.125, "maker_pct": 0.06},
    {"min_volume_usd": 1_000_000,  "taker_pct": 0.10,  "maker_pct": 0.04},
    {"min_volume_usd": 5_000_000,  "taker_pct": 0.04,  "maker_pct": 0.02},
    {"min_volume_usd": 10_000_000, "taker_pct": 0.03,  "maker_pct": 0.01},
    {"min_volume_usd": 25_000_000, "taker_pct": 0.03,  "maker_pct": 0.00},
]

# Market-maker routing: no commission, ~0.95% taken in the spread, per side.
DEFAULT_HALF_SPREAD_PCT = 0.95
DEFAULT_ROUND_TRIP_PCT = DEFAULT_HALF_SPREAD_PCT * 2


def fee_for_volume(volume_usd: float, style: str = "taker") -> float:
    """Percentage fee per side at a given trailing 30-day volume."""
    tier = FEE_TIERS[0]
    for t in FEE_TIERS:
        if volume_usd >= t["min_volume_usd"]:
            tier = t
    return tier["taker_pct"] if style == "taker" else tier["maker_pct"]
