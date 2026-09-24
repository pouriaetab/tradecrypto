"""The strategy-level Daily view counts what the tables hold, ranks peers per
day, and refuses to call two trades a trend.

Also pins a defect found while building it (2026-09-20): sizing.refit_all
selected `s.features` from signals, whose column is `features_json`, so the
conviction curve had silently failed to refit on every retrain pass.
"""
import datetime as dt
import time

from app.core import db
from app.research import strategy_days as sd


def _trade(strategy, symbol, day_offset_h, net, entry=1.0, qty=100.0):
    now = time.time() - day_offset_h * 3600
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (symbol, strategy, "paper", qty, entry, entry * (1 + net / (entry * qty)), now - 3600, now,
                3600, net + 1.9, 1.9, net))


def test_league_ranks_by_dollars_and_by_percent_separately(writable_db):
    # A trades often for small money; B trades once for a lot per trade.
    for i in range(6):
        _trade("A", f"C{i}", 2 + i * 0.1, +1.0)
    _trade("B", "ZZ", 2.5, +3.0, entry=1.0, qty=10.0)          # +30%/trade
    end = dt.datetime.now(sd.TZ).strftime("%Y-%m-%d")
    start = (dt.datetime.now(sd.TZ) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    r = sd.report(start, end)
    lg = {x["strategy"]: x for x in r["league"]}
    assert lg["A"]["rank_usd"] == 1 and lg["B"]["rank_pct"] == 1
    assert lg["A"]["n"] == 6 and abs(lg["A"]["net_usd"] - 6.0) < 1e-9
    day = [d for d in r["strategies"]["A"]["days"] if d["n"]][0]
    assert day["rank_usd"] == 1 and day["peers_that_day"] == 2


def test_two_trades_are_not_a_trend(writable_db):
    _trade("A", "C1", 30, +1.0)
    _trade("A", "C2", 2, +5.0)
    end = dt.datetime.now(sd.TZ).strftime("%Y-%m-%d")
    start = (dt.datetime.now(sd.TZ) - dt.timedelta(days=2)).strftime("%Y-%m-%d")
    r = sd.report(start, end)
    o = r["strategies"]["A"]["overall"]
    assert "not" in o["first_half_vs_second_half"]["verdict"] or "fewer" in o["first_half_vs_second_half"]["verdict"]


def test_change_log_reads_retrain_rows(writable_db):
    from app.research import retrain
    retrain.ensure_schema()
    db.execute("INSERT INTO model_versions(strategy, fitted_ts, params_json, train_json, holdout_json, full_json, "
               "is_champion, promoted, verdict, comparison, trigger, n_days_data, n_live_trades) "
               "VALUES ('A', ?, '{}', '{}', '{}', '{}', 1, 1, 'promoted: beat the champion', '{}', 'test', 10, 0)",
               (time.time() - 60,))
    _trade("A", "C1", 1, +1.0)
    end = dt.datetime.now(sd.TZ).strftime("%Y-%m-%d")
    r = sd.report(end, end)
    today = r["strategies"]["A"]["days"][-1]
    assert any(c["kind"] == "retrain" and c["promoted"] for c in today["changes"])
    assert r["strategies"]["A"]["overall"]["promotions"] == 1


def test_sizing_refit_reads_the_real_signals_column(writable_db):
    """`s.features` did not exist; every retrain logged 'sizing refit failed'."""
    from app.feedback import sizing
    out = sizing.refit_all("paper")
    assert "fitted" in out            # it ran; before the fix this raised OperationalError
