"""The freshest price we actually have for a coin, and how old it is.

WHY THIS EXISTS
---------------
The operator compared ARB in Robinhood against ARB in this app and found them a
cent and a half apart. He was right to ask: they should agree to within the
spread.

What he was shown was the last stored BAR close. Bars close on a schedule — an
hourly bar is written once an hour — so the "current" price on a screen could be
up to an hour old, and on 2026-09-18 it was forty minutes old:

    live quote (quotes table)   0.222760   3 seconds old
    last stored bar close       0.218280   40 MINUTES old   <- what was displayed
    minus the exit spread       0.216206   <- what the positions table showed

Meanwhile `quotes` had a three-second-old mid the whole time. The data was there;
nothing read it.

A stale price is not a cosmetic problem. It flows into unrealised P&L, into what
a position looks like it is worth, and into whether a number on screen can be
trusted at all — and it is silently wrong, which is the worst kind.

Every read goes through here, and every read carries its age so a caller can say
how old it is rather than implying it is now.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.core import db

# Past this, a quote is presented as an age rather than as the price.
STALE_AFTER_S = 120.0


@dataclass
class Price:
    symbol: str
    px: float | None
    age_s: float | None
    source: str

    @property
    def is_stale(self) -> bool:
        return self.age_s is None or self.age_s > STALE_AFTER_S

    def to_dict(self) -> dict:
        return {"px": self.px, "age_s": self.age_s,
                "source": self.source, "stale": self.is_stale}


def live_price(symbol: str) -> Price:
    """Freshest first: the live quote, then the newest bar, then nothing.

    The quote's MID, not its last trade: mid is what both sides of a spread are
    measured from, and it is what the cost model and the engine already use.
    """
    now = time.time()
    q = db.query_one(
        "SELECT ts, mid, last FROM quotes WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (symbol,))
    if q:
        px = q["mid"] if q["mid"] else q["last"]
        if px:
            return Price(symbol, float(px), now - float(q["ts"]), "quote")

    b = db.query_one(
        "SELECT ts, close FROM bars WHERE symbol=? AND close IS NOT NULL "
        "ORDER BY ts DESC LIMIT 1", (symbol,))
    if b and b["close"]:
        return Price(symbol, float(b["close"]), now - float(b["ts"]), "bar close")

    return Price(symbol, None, None, "none")


def live_prices(symbols: list[str]) -> dict[str, Price]:
    return {s: live_price(s) for s in symbols}
