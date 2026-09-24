"""The live mid is Robinhood's when Robinhood answers; Coinbase stays the
cross-check and keeps feeding the venue-tightness model; nothing downstream
loses a row.

2026-09-19: 54% of a 5.2 s tick was one Coinbase call plus one Kraken call per
open position, in sequence, and the price every stop was judged against was
not the venue's. One batched Robinhood call per tick fixes both -- provided
the Coinbase row keeps being written with its spread (symbol_cost reads it)
and price_basis never compares Robinhood with itself.
"""
import time

from app.core import db
from app.data.feeds import Quote
from app.execution import engine


def test_robinhood_row_parses_v1_fields():
    q = engine._rh_quote_from_row({"symbol": "DOGE-USD", "price": "0.10",
                                   "bid_inclusive_of_sell_spread": "0.09905",
                                   "ask_inclusive_of_buy_spread": "0.10095"})
    assert q.symbol == "DOGE" and q.source == "robinhood"
    assert abs(q.mid - 0.10) < 1e-9
    assert engine._rh_quote_from_row({"symbol": "X-USD"}) is None


def test_live_quote_prefers_a_fresh_robinhood_mid_and_keeps_the_coinbase_row(writable_db, monkeypatch):
    cb = Quote(symbol="DOGE", bid=0.0999, ask=0.1001, last=0.1, ts=time.time(), source="coinbase")
    rh = Quote(symbol="DOGE", bid=0.09905, ask=0.10095, last=0.10, ts=time.time(), source="robinhood")

    class _Feed:
        name = "coinbase"
        def quote(self, product): return cb
    monkeypatch.setattr(engine, "get_feed", lambda: _Feed())
    engine._rh_quotes.clear(); engine._cb_prefetch.clear()
    engine._rh_quotes["DOGE"] = (rh, time.time())

    q, agree = engine.live_quote("DOGE", "DOGE-USD")
    assert q.source == "robinhood" and agree["agree"]
    rows = db.query("SELECT source, spread_bps FROM quotes WHERE symbol='DOGE' ORDER BY id")
    assert [r["source"] for r in rows] == ["coinbase", "robinhood"]
    assert rows[0]["spread_bps"] is not None, "the venue-tightness input must survive"
    assert rows[1]["spread_bps"] is None, "Robinhood's markup is not a venue condition"


def test_a_stale_robinhood_quote_falls_back_to_coinbase(writable_db, monkeypatch):
    cb = Quote(symbol="DOGE", bid=0.0999, ask=0.1001, last=0.1, ts=time.time(), source="coinbase")
    rh = Quote(symbol="DOGE", bid=0.09905, ask=0.10095, last=0.10, ts=time.time(), source="robinhood")

    class _Feed:
        name = "coinbase"
        def quote(self, product): return cb
        def products(self): return {}
    monkeypatch.setattr(engine, "get_feed", lambda: _Feed())
    monkeypatch.setattr(engine, "get_fallback_feed", lambda: _Feed())
    engine._rh_quotes.clear(); engine._cb_prefetch.clear()
    engine._rh_quotes["DOGE"] = (rh, time.time() - engine.RH_QUOTE_TTL_S - 1)
    q, _ = engine.live_quote("DOGE", "DOGE-USD")
    assert q.source == "coinbase"
    assert db.query_one("SELECT COUNT(*) c FROM quotes WHERE source='robinhood'")["c"] == 0


def test_price_basis_never_compares_robinhood_with_itself(writable_db):
    from app.data import price_basis
    price_basis.ensure_schema()
    now = time.time()
    db.execute("INSERT INTO quotes(ts, symbol, bid, ask, mid, last, spread_bps, source) "
               "VALUES (?,?,?,?,?,?,?,?)", (now - 5, "DOGE", 0.0999, 0.1001, 0.1, 0.1, 20.0, "coinbase"))
    db.execute("INSERT INTO quotes(ts, symbol, bid, ask, mid, last, spread_bps, source) "
               "VALUES (?,?,?,?,?,?,?,?)", (now, "DOGE", 0.09905, 0.10095, 0.10, 0.10, None, "robinhood"))
    px, ts, src = price_basis._nearest_feed_price("DOGE", now)
    assert src == "coinbase"
