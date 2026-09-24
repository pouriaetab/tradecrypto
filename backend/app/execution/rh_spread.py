"""Robinhood's published crypto spread — the real, exact cost of a round trip.

Where this number comes from
----------------------------
Robinhood does not charge a commission. It charges a *spread*: it quotes you a
buy price above the mid and a sell price below it, and keeps the difference.
The order ticket states the number outright. Captured from the DOGE ticket:

    Mid price          $0.090569
    Bid price          $0.089704      sell spread 0.95%  ("includes 0.95% to Robinhood")
    Est buy price      $0.091485      buy  spread 0.95%

    (0.091485 - 0.090569) / 0.090569 = 0.95%   you pay 0.95% above mid
    (0.090569 - 0.089704) / 0.090569 = 0.95%   you receive 0.95% below mid

This replaces a guess. The old prior was 80 bps per side "from manual
observation", which understated the true cost by about 30 bps on a round trip —
and cost is the single biggest term in whether any of this is profitable.

Why the round trip is NOT simply 2 x spread
-------------------------------------------
You buy at m(1+s) and sell later at m'(1-s). Break-even needs

    m'(1-s) = m(1+s)   =>   m'/m = (1+s)/(1-s)

so the mid must RISE by (1+s)/(1-s) - 1, which is slightly more than 2s because
the loss on the way out is taken on a larger base. At s = 0.95% that is 1.9182%,
not 1.90%. Small, but this number is multiplied by every trade forever, and
there is no reason to carry an approximation when the exact form is one line.

The spread is per coin
----------------------
DOGE is 0.95%. Others differ — the operator has seen values above 1.01%. So this
is a table, seeded with a default and overwritten per coin with what the ticket
actually says. `source` records which: 'observed' is read off a real ticket,
'default' is the fallback. The UI shows that distinction rather than hiding it.
"""
from __future__ import annotations

import time

from app.core import db

# Seed value, from the DOGE ticket above. Used for a coin nobody has recorded yet.
DEFAULT_SPREAD_PCT = 0.95

# SEED VALUES ONLY. Both of the numbers below used to be the answer; they are now
# the answer only on a database with nothing measured in it. The operator's
# standing instruction is that a number nobody measured does not get to decide
# which trades happen, and a flat "required margin" is exactly that -- a tax
# invented to feel prudent.
#
# What replaces them:
#   * slippage is the median absolute shortfall this desk has ACTUALLY recorded
#     between deciding and filling (cost_observations.shortfall_bps);
#   * the margin is the UNCERTAINTY in the cost estimate itself, so it shrinks
#     as measurement improves instead of sitting at 30bps forever.
SLIPPAGE_SEED_BPS = 15.0
MARGIN_SEED_BPS = 30.0
# Enough observations before a measurement is preferred to its seed.
MIN_OBS = 20
# How far back to look. Spreads and latency both drift.
OBS_DAYS = 30.0

# Kept as module attributes because the Venue Lab page reads them by name. They
# are the seeds, not the live values -- `hurdle_breakdown()` reports what was
# actually used and where it came from.
SLIPPAGE_ALLOWANCE_BPS = SLIPPAGE_SEED_BPS
REQUIRED_MARGIN_BPS = MARGIN_SEED_BPS



def _active_mode() -> str:
    """Whichever book is running, so a live hurdle is never built from paper fills."""
    try:
        from app.core import mode as mode_mod
        return mode_mod.get_mode()
    except Exception:
        return "paper"


def _delay_components(symbol: str | None, mode: str | None = None) -> list[float]:
    """The part of the shortfall that is NOT the spread.

    This distinction matters and getting it wrong double-counts the largest cost
    in the system. `shortfall_bps` is the fill measured against the mid at
    DECISION, so it already contains the half-spread; `half_spread_bps` is the
    fill against the mid at SUBMIT, which is the spread alone. Using shortfall as
    a slippage term on top of the round trip charges the spread twice -- on this
    database that read as 88bps of "slippage" against a 95bps half-spread, which
    is the spread wearing a different hat.

    The drift between deciding and submitting is the difference of the two, and
    that is the only part the round trip does not already cover.
    """
    import time as _t
    since = _t.time() - OBS_DAYS * 86400.0
    sql = ("SELECT shortfall_bps, half_spread_bps FROM cost_observations "
           "WHERE ts >= ? AND shortfall_bps IS NOT NULL AND half_spread_bps IS NOT NULL")
    args: tuple = (since,)
    if symbol:
        sql += " AND symbol = ?"
        args = args + (symbol.upper(),)
    if mode:
        sql += " AND mode = ?"
        args = args + (mode,)
    return [abs(float(r["shortfall_bps"]) - float(r["half_spread_bps"]))
            for r in db.query(sql, args)]


def _sample(symbol: str | None, mode: str | None = None) -> tuple[list[float], str]:
    """This coin's own drift if there is enough of it, otherwise the desk's.

    MODE MATTERS HERE and getting it wrong would be dangerous in exactly one
    direction. The paper simulator fills at the same instant it decides, so its
    delay component is zero BY CONSTRUCTION -- not because execution is fast. If
    live sizing ever read that zero it would believe real fills arrive with no
    drift, which is the one assumption that cannot be checked until money is
    already at risk. So live asks only for live fills, and falls back to the
    conservative seed when it has none of its own.
    """
    for scope, label in ((symbol, f"on {symbol}"), (None, "desk-wide")):
        obs = _delay_components(scope, mode) if (scope or True) else []
        if len(obs) >= MIN_OBS:
            suffix = " (paper fills have no latency by construction)" \
                if mode == "paper" else ""
            return obs, f"measured {label} ({len(obs)} fills){suffix}"
    return [], (f"seed — no {mode or 'recorded'} fills to measure from"
                if mode != "paper" else "seed — no paper fills yet")


def slippage_bps(symbol: str | None = None,
                 mode: str | None = None) -> tuple[float, str]:
    """Measured drift between the decision mid and the fill, over and above the
    spread the round trip already charges.

    Per coin when there is enough of it, desk-wide when there is not, the seed
    only when there is nothing. The median rather than the mean: one bad fill
    during a news spike should not permanently raise the bar on every later trade.
    """
    import statistics
    obs, src = _sample(symbol, mode or _active_mode())
    if src.startswith("seed"):
        return SLIPPAGE_SEED_BPS, src
    return float(statistics.median(obs)), src


def margin_bps(symbol: str | None = None,
               mode: str | None = None) -> tuple[float, str]:
    """How wrong the cost estimate could be, in place of an invented margin.

    A flat "profit we insist on keeping" is a number someone chose. What actually
    justifies asking for more than breakeven is that breakeven is ESTIMATED: costs
    vary between fills, so a rule that clears the point estimate may not clear the
    truth. The margin is therefore one standard error of the measured cost --
    large while little is known, shrinking as the measurement firms up, and never
    a permanent tax on every trade.
    """
    import math
    import statistics
    obs, src = _sample(symbol, mode or _active_mode())
    if src.startswith("seed") or len(obs) < 2:
        return MARGIN_SEED_BPS, src.replace("only", "uncertainty not estimable —")
    sd = statistics.pstdev(obs)
    se = sd / math.sqrt(len(obs))
    return float(se), f"one standard error of the measured cost ({src}, sd {sd:.1f}bps)"


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS rh_spreads (
        symbol      TEXT PRIMARY KEY,
        spread_pct  REAL NOT NULL,
        source      TEXT NOT NULL DEFAULT 'observed',
        observed_at REAL NOT NULL,
        note        TEXT
    )""")


def round_trip_bps(spread_pct: float) -> float:
    """Exact percentage the mid must rise before a round trip breaks even."""
    s = float(spread_pct) / 100.0
    if s <= 0:
        return 0.0
    if s >= 0.5:                       # nonsense input; refuse rather than divide by ~0
        raise ValueError(f"spread of {spread_pct}% per side is not plausible")
    return ((1.0 + s) / (1.0 - s) - 1.0) * 1e4


def get(symbol: str) -> dict:
    """The spread for one coin, always with its provenance attached."""
    ensure_schema()
    row = db.query_one("SELECT * FROM rh_spreads WHERE symbol=?", (symbol.upper(),))
    if row:
        pct, src, at, note = row["spread_pct"], row["source"], row["observed_at"], row["note"]
    else:
        pct, src, at, note = DEFAULT_SPREAD_PCT, "default", None, None
    rt = round_trip_bps(pct)
    return {
        "symbol": symbol.upper(),
        "spread_pct": pct,
        "per_side_bps": pct * 100.0,
        "round_trip_bps": rt,
        "round_trip_pct": rt / 100.0,
        "source": src,
        "observed_at": at,
        "note": note,
        "is_measured": src == "observed",
        "explain": (
            f"Buy fills at mid x {1 + pct/100:.6f}, sell at mid x {1 - pct/100:.6f}. "
            f"The mid must rise {rt/100:.3f}% before you are even."
            + ("" if src == "observed" else
               f"  NOT verified for {symbol.upper()} — this is the {DEFAULT_SPREAD_PCT}% "
               f"default. Open the order ticket and record the real number.")
        ),
    }


def set_spread(symbol: str, spread_pct: float, source: str = "observed",
               note: str | None = None) -> dict:
    round_trip_bps(spread_pct)                     # validates, raises on nonsense
    ensure_schema()
    db.execute(
        """INSERT INTO rh_spreads(symbol, spread_pct, source, observed_at, note)
           VALUES (?,?,?,?,?)
           ON CONFLICT(symbol) DO UPDATE SET
             spread_pct=excluded.spread_pct, source=excluded.source,
             observed_at=excluded.observed_at, note=excluded.note""",
        (symbol.upper(), float(spread_pct), source, time.time(), note))
    db.log_event("INFO", "cost",
                 f"Robinhood spread for {symbol.upper()} recorded as {spread_pct}% per side "
                 f"({round_trip_bps(spread_pct)/100:.3f}% round trip)")
    return get(symbol)


def hurdle_bps(symbol: str) -> float:
    """What a signal must beat. Every term is named and every term is measured."""
    slip, _ = slippage_bps(symbol)
    marg, _ = margin_bps(symbol)
    return get(symbol)["round_trip_bps"] + slip + marg


def hurdle_breakdown(symbol: str) -> dict:
    g = get(symbol)
    rt = g["round_trip_bps"]
    slip, slip_src = slippage_bps(symbol)
    marg, marg_src = margin_bps(symbol)
    total = rt + slip + marg
    return {
        **g,
        "components": [
            {"name": "Robinhood spread, round trip", "bps": rt,
             "why": "Published on the order ticket. Exact, not estimated.",
             "certain": g["source"] == "observed"},
            {"name": "Slippage", "bps": slip,
             "why": f"How far the mid actually moved between deciding and filling: "
                    f"{slip_src}.", "certain": slip_src.startswith("measured")},
            {"name": "Uncertainty in the cost estimate", "bps": marg,
             "why": f"Breakeven is estimated, not known, so a rule that clears the "
                    f"point estimate may not clear the truth: {marg_src}. This "
                    f"shrinks as measurement improves; it is not a fixed tax.",
             "certain": marg_src.startswith("one standard error")},
        ],
        "hurdle_bps": total,
        "hurdle_pct": total / 100.0,
        "plain": (f"A {symbol.upper()} trade must gain more than {total/100:.2f}% "
                  f"gross to be worth taking."),
    }


def table(symbols: list[str] | None = None) -> list[dict]:
    """Every coin's spread, for the Cost Lab. Unrecorded coins are listed too,
    flagged as default, because a missing row is the thing worth acting on."""
    ensure_schema()
    if symbols is None:
        symbols = [r["symbol"] for r in
                   db.query("SELECT symbol FROM universe WHERE active=1 ORDER BY symbol")]
    return [get(s) for s in symbols]
