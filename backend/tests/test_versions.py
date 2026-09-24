"""Every trade knows the rule that made it; the learning loop discounts trades
from superseded rules by how different those rules were; the stability report
says when to stop discounting. research/versions.py.
"""
import time

import numpy as np

from app.core import db
from app.research import versions
from app.research.stats import NormalInverseGamma


def test_hash_ignores_fit_bookkeeping_and_sees_rule_changes():
    a = {"climb_pct": 3.0, "calib_n": 12, "calib_note": "x", "expected_edge_bps": 0.0}
    b = {"climb_pct": 3.0, "calib_n": 99, "calib_note": "y", "expected_edge_bps": 5.0}
    c = {"climb_pct": 4.0, "calib_n": 12}
    assert versions.params_hash(a) == versions.params_hash(b)
    assert versions.params_hash(a) != versions.params_hash(c)


def test_similarity_is_the_shared_parameter_share():
    cur = {f"p{i}": i for i in range(12)}
    tuned = {**cur, "p3": 99}                     # one of twelve moved
    rewrite = {**{f"p{i}": -1 for i in range(10)}, "p10": 10, "p11": 11}
    assert abs(versions.similarity(tuned, cur) - 11 / 12) < 1e-9
    assert abs(versions.similarity(rewrite, cur) - 2 / 12) < 1e-9


def test_a_closed_trade_is_tagged_with_the_current_rule(writable_db):
    versions.ensure_schema()
    now = time.time()
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) VALUES ('AAA','day_climb','paper',1,1,1.01,?,?,60,1,0.5,0.5)",
               (now - 60, now))
    tid = db.query_one("SELECT id FROM trades ORDER BY id DESC LIMIT 1")["id"]
    cur = versions.tag_trade(tid, "day_climb")
    row = db.query_one("SELECT strategy_version, params_hash FROM trades WHERE id=?", (tid,))
    assert row["params_hash"] == cur["params_hash"] and row["strategy_version"] == cur["version"]
    assert not row["params_hash"].endswith("*")


def test_weighted_posterior_counts_a_discounted_trade_for_less():
    x = [0.02, -0.01, 0.03, 0.01]
    full = NormalInverseGamma().update(x)
    half = NormalInverseGamma().update(x, weights=[1, 1, 0.5, 0.5])
    none = NormalInverseGamma().update(x, weights=[1, 1, 0, 0])
    assert abs((full.kappa - 1) - 4.0) < 1e-9
    assert abs((half.kappa - 1) - 3.0) < 1e-9
    assert abs((none.kappa - 1) - 2.0) < 1e-9
    assert abs(none.mean - NormalInverseGamma().update(x[:2]).mean) < 1e-12


def test_a_removed_version_weighs_zero_and_a_tuning_weighs_nearly_one(writable_db, monkeypatch):
    versions.ensure_schema()
    now = time.time()
    cur = versions.current("day_climb")
    # three trades: one on the current rule, one on a lightly tuned earlier
    # rule, one on a rule that is gone from the chain entirely
    base = dict(cur["params"])
    tuned = {**base, "climb_min_pct": base.get("climb_min_pct", 5.0) + 1.0}
    h_tuned = versions.params_hash(tuned)
    for h, t in ((h_tuned, now - 3000), (cur["params_hash"], now - 1000)):
        db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
                   "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, strategy_version, params_hash) "
                   "VALUES ('AAA','day_climb','paper',1,1,1.01,?,?,60,1,0.5,0.5,'0.1',?)", (t - 60, t, h))
    # the earlier rule's parameters are on record in model_versions
    from app.research import retrain
    retrain.ensure_schema()
    import json
    db.execute("INSERT INTO model_versions(strategy, fitted_ts, params_json, promoted, is_champion) "
               "VALUES ('day_climb', ?, ?, 0, 0)", (now - 4000, json.dumps({"climb_min_pct": tuned["climb_min_pct"]})))
    rows = [{"params_hash": h_tuned}, {"params_hash": cur["params_hash"]}, {"params_hash": "deadbeef0000"}]
    w = versions.weights_for("day_climb", rows, stable=False)
    assert w[1] == 1.0
    assert 0.85 <= w[0] < 1.0, w
    assert w[2] == 0.0
    assert np.all(versions.weights_for("day_climb", rows, stable=True) == 1.0)


def test_stability_report_has_the_three_readings(writable_db):
    versions.ensure_schema()
    r = versions.stability("day_climb")
    assert set(r) >= {"a_promotion_rate_per_100", "b_param_drift", "c_holdout_consistency", "stable"}
    assert r["stable"] is False          # nothing has been promoted; (c) cannot hold yet
