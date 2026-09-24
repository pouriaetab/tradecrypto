"""Polymarket crypto markets: parsed without the network, read as a distribution.

The fetch itself is exercised against a canned Gamma payload through a fake
client, so the suite never touches the internet and the parser is pinned to
the exact shape the API returned on 2026-09-21.
"""
from __future__ import annotations

import json

import httpx

from app.core import db
from app.data import prediction_markets as pm

GAMMA_SAMPLE = [
    {"id": "4558867", "question": "Will the price of Bitcoin be above $86,000 on September 21?",
     "endDate": "2026-09-21T12:00:00Z", "outcomePrices": '["0.2835", "0.7165"]',
     "volume24hr": "258569.1", "liquidity": "31534.0"},
    {"id": "4558864", "question": "Will the price of Bitcoin be above $84,000 on September 21?",
     "endDate": "2026-09-21T12:00:00Z", "outcomePrices": '["0.995", "0.005"]',
     "volume24hr": "306360", "liquidity": "33093"},
    {"id": "4558869", "question": "Will the price of Bitcoin be above $88,000 on September 21?",
     "endDate": "2026-09-21T12:00:00Z", "outcomePrices": '["0.0015", "0.9985"]',
     "volume24hr": "175823", "liquidity": "56061"},
    {"id": "1339768", "question": "Will Bitcoin dip to $40,000 by December 31, 2026?",
     "endDate": "2027-01-01T05:00:00Z", "outcomePrices": '["0.055", "0.945"]',
     "volume24hr": "164954", "liquidity": "88560"},
    {"id": "4052450", "question": "Will Ethereum reach $2,800 in September?",
     "endDate": "2026-10-01T00:00:00Z", "outcomePrices": '["0.725", "0.275"]',
     "volume24hr": "148662", "liquidity": "27058"},
    {"id": "4727187", "question": "Bitcoin Up or Down on September 21?",
     "endDate": "2026-09-21T12:00:00Z", "outcomePrices": '["0.9985", "0.0015"]',
     "volume24hr": "122384", "liquidity": "19187"},
    {"id": "1163699", "question": "Clarity Act (H.R.3633) signed into law in 2026?",
     "endDate": "2027-01-01T05:00:00Z", "outcomePrices": '["0.06", "0.94"]',
     "volume24hr": "218102", "liquidity": "457401"},
    {"id": "junk", "question": "No prices here", "outcomePrices": None},
]


def test_questions_are_classified_by_asset_kind_and_strike():
    c = pm.classify("Will the price of Bitcoin be above $86,000 on September 21?")
    assert c == {"asset": "BTC", "kind": "above_on_day", "threshold": 86000.0}
    assert pm.classify("Will Ethereum reach $2,800 in September?") == {
        "asset": "ETH", "kind": "reach_by", "threshold": 2800.0}
    assert pm.classify("Will Bitcoin dip to $40,000 by December 31, 2026?")["kind"] == "dip_to"
    assert pm.classify("Bitcoin Up or Down on September 21?")["kind"] == "up_or_down"
    assert pm.classify("Will Bitcoin hit $150k by December 31, 2026?")["threshold"] == 150000.0
    assert pm.classify("Clarity Act (H.R.3633) signed into law in 2026?")["asset"] is None


def test_a_ladder_reads_as_a_distribution():
    rows = [{"threshold": 84000, "p_yes": 0.995}, {"threshold": 86000, "p_yes": 0.2835},
            {"threshold": 88000, "p_yes": 0.0015}]
    imp = pm.implied_from_ladder(rows)
    assert imp is not None
    assert 85000 < imp["median"] < 86000          # the crossing sits just under 86k
    assert imp["p10"] < imp["median"] < imp["p90"]
    assert pm.implied_from_ladder(rows[:2]) is None   # two strikes is not a ladder


def test_fetch_stores_every_priced_market_and_latest_reads_it_back(writable_db):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("user-agent") == pm.USER_AGENT, "Gamma 403s the default UA"
        assert request.url.params["tag_id"] == str(pm.CRYPTO_TAG_ID)
        return httpx.Response(200, json=GAMMA_SAMPLE)

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": pm.USER_AGENT})
    out = pm.fetch(client=client)
    assert out["ok"] and out["stored"] == 7, out          # the junk row has no price
    n = db.query_one("SELECT COUNT(*) c FROM prediction_markets")["c"]
    assert n == 7

    import datetime as _dt
    from unittest import mock
    fake_today = "2026-09-21"
    with mock.patch.object(pm, "datetime") as dtm:
        dtm.now.return_value = _dt.datetime(2026, 9, 21, 15, 0, tzinfo=_dt.timezone.utc)
        latest = pm.latest()
    assert latest["available"] and latest["n_markets"] == 7
    btc = latest["today"]["BTC"]
    assert btc["resolves"] == fake_today and btc["strikes"] == 3
    assert 85000 < btc["median"] < 86000
    kinds = {r["kind"] for r in latest["longer_dated"]}
    assert kinds == {"reach_by", "dip_to"}
    assert "not an entry input" in latest["note"]


def test_a_refused_fetch_is_reported_not_raised(writable_db):
    def handler(request):
        return httpx.Response(403, text="forbidden")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = pm.fetch(client=client)
    assert out["ok"] is False and "403" in out["error"]
    assert db.query_one("SELECT COUNT(*) c FROM prediction_markets")["c"] == 0
