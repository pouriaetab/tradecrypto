"""The exit lab back-fills a rule added later, and judges every rule against
what the desk actually booked -- not against one rule it declared "live".

2026-09-19: the ride_* and flat_by_* families sat at n=8 beside rules at n=39
because `absorb()` keyed "done" on trade_id alone; and the baseline row said
-0.96%/trade while the desk had actually made +1.12%/trade on the same trades
with its real exits (fixed targets, catastrophe stops, time limits). Every
"vs live" figure on the page was measured against a rule that was not running.
"""
import time

from app.core import db
from app.research import exit_lab


def _seed(writable_db):
    t0 = time.time() - 30 * 3600
    for i in range(30):
        ts = t0 + i * 60
        px = 1.0 + 0.001 * i
        db.execute("INSERT INTO bars(symbol, granularity, ts, open, high, low, close, volume, source) "
                   "VALUES ('AAA', 60, ?, ?, ?, ?, ?, 1000, 'test')", (ts, px, px * 1.001, px * 0.999, px))
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) "
               "VALUES ('AAA','day_climb','paper',100,1.0,1.02,?,?,1740,2.0,1.9,0.10)",
               (t0, t0 + 29 * 60))


def test_a_rule_added_later_is_replayed_on_earlier_trades(writable_db, monkeypatch):
    _seed(writable_db)
    exit_lab.absorb()
    before = db.query_one("SELECT COUNT(DISTINCT rule) c FROM exit_counterfactuals")["c"]
    # + BOUNDS: replay() also emits the two hindsight bounds (_ceiling_peak,
    # _floor_trough) beside the rules. They are not rules and are not tradeable;
    # they are counted here so this assertion stays about "every rule ran" rather
    # than becoming a magic number that drifts.
    assert before == len(exit_lab.RULES) + len(exit_lab.SCALE_RULES) + len(exit_lab.BOUNDS)
    monkeypatch.setitem(exit_lab.RULES, "zz_new_rule",
                        {"fn": exit_lab._mk(trail=0.03), "what": "added later"})
    out = exit_lab.absorb()
    assert out["replayed"] == 1, "the old trade must be replayed for the new rule"
    assert db.query_one("SELECT COUNT(*) c FROM exit_counterfactuals WHERE rule='zz_new_rule'")["c"] == 1


def test_the_bounds_are_back_filled_onto_trades_already_replayed(writable_db):
    """2026-09-22: the two hindsight bounds were added after 113 trades had been
    replayed. `want` did not mention them, so every one of those trades counted
    as done and the ceiling would have existed only for trades closing later --
    n=2 beside rules at n=113. Deleting the bound rows must bring them back."""
    _seed(writable_db)
    exit_lab.absorb()
    for b in exit_lab.BOUNDS:
        assert db.query_one("SELECT COUNT(*) c FROM exit_counterfactuals WHERE rule=?", (b,))["c"] == 1
    db.execute("DELETE FROM exit_counterfactuals WHERE rule IN (?, ?)", exit_lab.BOUNDS)
    assert exit_lab.absorb()["replayed"] == 1, "a missing bound did not make the trade replayable"
    for b in exit_lab.BOUNDS:
        assert db.query_one("SELECT COUNT(*) c FROM exit_counterfactuals WHERE rule=?", (b,))["c"] == 1


def test_the_baseline_is_what_was_actually_booked(writable_db):
    _seed(writable_db)
    exit_lab.absorb()
    st = exit_lab.standings()
    live = [r for r in st["rules"] if r["is_live_rule"]]
    assert len(live) == 1 and live[0]["rule"] == "as_traded"
    # +0.10 net on a $100 basis = +0.10%
    assert abs(live[0]["mean_net_pct"] - 0.10) < 1e-9
    assert not any(r["is_live_rule"] for r in st["rules"] if r["rule"] == "trail_8_breakeven")


def test_standings_reports_the_bounds_and_never_recommends_them(writable_db):
    """2026-09-23: 262 bound rows sat in the database while the page showed 47
    rules and no ceiling -- standings() looped over RULES, which the bounds are
    deliberately not in. Same shape as the `want` bug one day earlier: a new
    output added to replay() and not to the place that READS replay()."""
    _seed(writable_db)
    exit_lab.absorb()
    rows = {r["rule"]: r for r in exit_lab.standings()["rules"]}
    for b in exit_lab.BOUNDS:
        assert b in rows, f"{b} is in the database but missing from the standings"
        assert rows[b]["what"], f"{b} reaches the page with no explanation of what it is"
        assert rows[b]["tradeable"] is False
        assert "hindsight" in rows[b]["verdict"], \
            "a hindsight bound carries a verdict that reads as a recommendation"
    live = [r for r in rows.values() if r.get("is_live_rule")]
    assert all(r["tradeable"] for r in live), "a bound was marked as the live rule"
    assert rows["hold_to_close"]["tradeable"] is True
