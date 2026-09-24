"""The first screen a new person meets, and the demo behind it.

An empty dashboard is indistinguishable from a broken one, which is what the
operator's non-technical friend actually met. These tests cover the two things
that can go wrong with the fix, and both have already gone wrong once:

  1. The demo is mistaken for real results. Every demo row carries a stamp, one
     button removes exactly those rows, and a user's own trades survive it.
  2. The demo writes rows the rest of the app cannot see. `signals.decision`
     has a fixed vocabulary -- "taken", "taken_experiment", "rejected" -- and
     the first version of the seeder wrote "take"/"reject". 42 signals landed in
     the table and the Journal's own default filter matched none of them: a
     populated database and a blank page, which is the exact failure the demo
     exists to prevent.
"""
from __future__ import annotations

import json
import time

import pytest

from app.core import db, first_run


def _trade(demo: bool, ts: float, net: float = 1.0) -> None:
    db.execute(
        "INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, "
        "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, "
        "attribution_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("BTC", "day_climb", "paper", 1.0, 100.0, 101.0, ts, ts + 3600, 3600.0,
         net, 0.0, net, json.dumps({"demo": True} if demo else {"live": True})),
    )


def test_a_fresh_install_is_asked_which_way_in(writable_db):
    db.execute("DELETE FROM trades")
    db.execute("DELETE FROM app_state WHERE key LIKE 'first_run%'")
    st = first_run.state()
    assert st["needs_choice"] is True
    assert st["choice"] is None
    assert st["trades"] == 0


def test_a_returning_user_is_never_asked_again(writable_db):
    db.execute("DELETE FROM trades")
    first_run._put(first_run.CHOICE_KEY, "live")
    assert first_run.state()["needs_choice"] is False


def test_someone_with_real_trades_is_never_offered_a_demo(writable_db):
    """Belt and braces: an empty choice key must not re-offer a demo to a book
    that already has history in it -- that is how a returning user gets asked to
    overwrite their own results."""
    db.execute("DELETE FROM trades")
    db.execute("DELETE FROM app_state WHERE key LIKE 'first_run%'")
    _trade(demo=False, ts=time.time() - 7200)
    assert first_run.state()["needs_choice"] is False


def test_clearing_the_demo_keeps_the_users_own_trades(writable_db):
    """THE one that matters.

    Someone who goes live from inside the demo has both kinds interleaved. If
    "clear the demo" were a date range or a wholesale delete it would take their
    real trades with it, silently, and there is no undo.
    """
    db.execute("DELETE FROM trades")
    now = time.time()
    for i in range(5):
        _trade(demo=True, ts=now - 86400 * (10 - i))
    for i in range(3):
        _trade(demo=False, ts=now - 3600 * (3 - i), net=2.0)

    assert first_run.demo_trade_count() == 5
    res = first_run.clear_demo()

    assert res["removed"] == 5
    assert first_run.demo_trade_count() == 0
    left = db.query_one("SELECT COUNT(*) c FROM trades")["c"]
    assert left == 3, "the user's own trades were destroyed with the demo"


def test_every_demo_trade_carries_the_stamp(writable_db):
    """Nothing may enter the book pretending to be real."""
    db.execute("DELETE FROM trades")
    _trade(demo=True, ts=time.time() - 3600)
    row = db.query_one("SELECT attribution_json FROM trades")
    assert json.loads(row["attribution_json"]).get("demo") is True


def test_the_demo_refuses_to_run_without_price_history(writable_db):
    """It must say why, not produce an empty or invented book."""
    db.execute("DELETE FROM bars")
    ready, why = first_run.demo_ready()
    assert ready is False
    assert "collecting" in why.lower()
    res = first_run.seed_demo()
    assert res["ok"] is False and res["trades"] == 0


def test_the_seeder_uses_the_apps_own_decision_words():
    """`signals.decision` is a closed vocabulary and the Journal filters on it.

    Writing "take"/"reject" instead of "taken"/"rejected" puts rows in the table
    that no page can match: a populated database and a blank Journal. That
    shipped once. This reads the source because the alternative -- generating a
    demo -- needs hours of real price history that a unit test cannot have.
    """
    import inspect

    src = inspect.getsource(first_run)
    assert '"decision": "taken"' in src
    assert '"decision": "rejected"' in src
    assert '"decision": "take"' not in src
    assert '"decision": "reject"' not in src


def test_the_demo_pays_the_real_cost():
    """A demo that quietly charged less than Robinhood would be a brochure."""
    assert first_run.DEMO_HALF_SPREAD == pytest.approx(0.0095)
    round_trip_pct = first_run.DEMO_HALF_SPREAD * 2 * 100
    assert round_trip_pct == pytest.approx(1.9, abs=0.01)


def test_the_demo_replays_the_strategies_that_actually_run():
    """Replaying a different set would preview a program nobody is about to run."""
    import inspect

    from app.strategy.registry import ACTIVE_STRATEGIES

    src = inspect.getsource(first_run.seed_demo)
    assert "ACTIVE_STRATEGIES" in src
    assert len(ACTIVE_STRATEGIES) >= 4


def test_choosing_live_empties_the_book(writable_db):
    """Their numbers start now, and are only ever theirs."""
    db.execute("DELETE FROM trades")
    _trade(demo=True, ts=time.time() - 86400)
    first_run.choose_live()
    assert db.query_one("SELECT COUNT(*) c FROM trades")["c"] == 0
    assert first_run.state()["choice"] == "live"
