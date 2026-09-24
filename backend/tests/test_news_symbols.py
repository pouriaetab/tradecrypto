"""A headline is about a coin only if it is actually about that coin.

    "currently i see this in news & research 'Do not trade: NEAR ...' this is
     not what i told you ... i didnt say to not trade NEAR anymore, so what is
     this!!!???"

The headline was:

    "Robinhood Chain fees collapse 97% even as transactions stay near record highs"

The matcher lowercased the text and looked for each ticker as a word, so the
English word "near" matched the NEAR ticker and "collapse" pushed it to
CRITICAL. The app then advised against trading a coin he was holding, on the
strength of a story about Robinhood's chain fees.

Across the stored history, 37 of 307 tagged headlines were wrong this way:
TRUMP x22 (the politician), NEAR x14 (the preposition), DOT x1.
"""
from __future__ import annotations

import pytest

from app.data.news import match_symbols

U = ["NEAR", "BTC", "SOL", "LINK", "TRUMP", "DOT", "ZEC", "ARB"]


class TestTheEnglishWordIsNotTheTicker:

    @pytest.mark.parametrize("headline", [
        "Robinhood Chain fees collapse 97% even as transactions stay near record highs",
        "Bitcoin coils near $76.5K as US stocks rebound",
        "Near-term outlook remains cautious",
        "Analysts see support near the 200-day average",
    ])
    def test_the_word_near_does_not_flag_the_near_coin(self, headline):
        assert "NEAR" not in match_symbols(headline, U), headline

    @pytest.mark.parametrize("headline", [
        "Trump signs executive order on digital assets",
        "Markets rally as Trump comments on tariffs",
    ])
    def test_the_politician_does_not_flag_the_memecoin(self, headline):
        """22 of these were tagged, on a coin the desk was holding."""
        assert "TRUMP" not in match_symbols(headline, U), headline

    @pytest.mark.parametrize("headline,want", [
        ("NEAR Protocol jumps 12% after mainnet upgrade", "NEAR"),
        ("$NEAR rallies on the AI narrative", "NEAR"),
        ("ARB holders vote on the treasury proposal", "ARB"),
        ("TRUMP memecoin slides 30% after the unlock", "TRUMP"),
    ])
    def test_a_real_mention_still_matches(self, headline, want):
        """The fix must not make the feature useless — tickers as people write
        them, in capitals or with a $, still count."""
        assert want in match_symbols(headline, U), headline

    def test_a_full_name_matches_however_it_is_written(self):
        """Nobody writes "chainlink" by accident, so aliases stay case-insensitive."""
        assert "LINK" in match_symbols("chainlink oracles expand to new chains", U)
        assert "LINK" in match_symbols("Chainlink Oracles Expand", U)

    def test_a_shouted_headline_falls_back_to_names_only(self):
        """AN ALL-CAPS HEADLINE would match every English-word ticker."""
        got = match_symbols("BITCOIN SOARS AS TRADERS STAY NEAR THE HIGHS", U)
        assert "NEAR" not in got

    def test_the_matcher_is_case_sensitive_on_bare_tickers(self):
        """The whole fix in one assertion."""
        assert match_symbols("the price stayed near the high", U) == []
        assert "NEAR" in match_symbols("the price of NEAR stayed high", U)


class TestStoredRowsAreRetaggedWhenTheMatcherChanges:
    """Fixing the matcher did not fix the banner: the NEAR false positive was
    already stored, and alerts() reads what is stored. 2026-09-19 the "Do not
    trade: NEAR" banner outlived the fix by a day for exactly that reason."""

    def test_retag_rewrites_a_stored_false_positive_once(self, writable_db):
        import time
        from app.core import db
        from app.data import news
        from app.core import mode
        news.ensure_schema(); mode._ensure()
        db.execute("DELETE FROM app_state WHERE key='news_matcher_version'")
        db.execute("DELETE FROM news")
        db.execute(
            "INSERT INTO news(guid, ts, fetched_ts, source, title, link, summary, symbols, "
            "categories, severity) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("g1", time.time(), time.time(), "t",
             "Robinhood Chain fees collapse 97% even as transactions stay near record highs",
             "", "", "NEAR", "bankruptcy", "critical"))
        assert news.retag_stored(["NEAR", "BTC"]) == 1
        assert db.query_one("SELECT symbols FROM news WHERE guid='g1'")["symbols"] == ""
        assert "NEAR" not in news.alerts(24)["flagged_symbols"]
        # second call is a no-op: the version is recorded
        assert news.retag_stored(["NEAR", "BTC"]) == 0
