"""The regime layer: day types come from the data, the router changes nothing
until a regime cell is measurably different, and never more than x1.5 / x0.5.
"""
import json
import time

import numpy as np

from app.core import db
from app.research import regime_days as rd


def test_mixture_recovers_planted_day_types():
    rng = np.random.default_rng(1)
    rows = []
    for i in range(300):
        bull = i % 2 == 0
        rows.append({"date": f"d{i}", "breadth": (0.7 if bull else 0.3) + rng.normal(0, 0.05),
                     "median_24h": (2.0 if bull else -2.0) + rng.normal(0, 0.4),
                     "btc_24h": (1.5 if bull else -1.5) + rng.normal(0, 0.4),
                     "dispersion": 3.0 + rng.normal(0, 0.3), "vol_24h": 2.0 + rng.normal(0, 0.2),
                     "day_so_far": (0.5 if bull else -0.5) + rng.normal(0, 0.2)})
    fit = rd.fit_regimes(rows, k_range=(2, 3, 4))
    assert fit["available"] and fit["k"] == 2
    names = set(fit["names"])
    assert any(n.startswith("bull") for n in names) and any(n.startswith("bear") for n in names)
    # classify() agrees with the fitted labels
    name, p = rd.classify(fit, rows[0])
    assert name == fit["labels"]["d0"] and p > 0.9


def _plant(writable_db, cell_mean, cell_n, overall_mean=-1.0, overall_n=400, se=0.2):
    today = __import__("datetime").datetime.now(rd.TZ).date().isoformat()
    res = {"available": True, "scorecard": {"day_climb": {
        "overall": {"n": overall_n, "mean_net_pct": overall_mean, "se_pct": se / 4},
        "by_regime": {"bear-narrow": {"n": cell_n, "mean_net_pct": cell_mean, "se_pct": se,
                                      "sigmas": (cell_mean - overall_mean) / (se * 1.03)}}}}}
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "regime_days", "t", "{}", json.dumps(res), ""))
    from app.core import mode
    mode._ensure()
    db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES ('regime_today', ?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
               (json.dumps({"date": today, "regime": "bear-narrow", "p": 0.9}), time.time()))


def test_router_is_one_when_the_cell_is_indistinguishable(writable_db):
    _plant(writable_db, cell_mean=-1.05, cell_n=60)          # 0.25σ away
    m = rd.multiplier("day_climb")
    assert m["multiplier"] == 1.0 and "indistinguishable" in m["why"], m


def test_router_shrinks_a_measurably_worse_regime_and_never_below_half(writable_db):
    _plant(writable_db, cell_mean=-2.4, cell_n=2000)          # ~7σ worse, thick cell
    m = rd.multiplier("day_climb")
    assert m["multiplier"] == 0.5 and m["regime"] == "bear-narrow"


def test_router_grows_a_measurably_better_regime_but_is_shrunk_by_a_thin_cell(writable_db):
    _plant(writable_db, cell_mean=0.0, cell_n=10)             # 5σ better, only 10 entries
    m = rd.multiplier("day_climb")
    assert 1.0 < m["multiplier"] < 1.25, m                    # 10/(10+40) shrink


def test_no_reading_for_today_means_no_change(writable_db):
    from app.core import mode
    mode._ensure()
    db.execute("DELETE FROM app_state WHERE key='regime_today'")
    assert rd.multiplier("day_climb")["multiplier"] == 1.0


def test_size_multiplier_reaches_the_notional_and_stays_inside_the_caps(writable_db, monkeypatch):
    from app.feedback import loop
    from app.core import mode
    mode._ensure(); mode.set_equity(2000.0)
    db.execute("DELETE FROM positions"); db.execute("DELETE FROM equity_curve")
    db.execute("INSERT INTO equity_curve(ts, mode, equity, cash, positions_value, realised_pnl, unrealised_pnl) "
               "VALUES (?,?,?,?,?,?,?)", (time.time(), "paper", 2000.0, 2000.0, 0.0, 0.0, 0.0))
    base = loop.position_size_usd("day_climb", "paper", stop_bps=800.0)
    half = loop.position_size_usd("day_climb", "paper", stop_bps=800.0, size_mult=0.5)
    more = loop.position_size_usd("day_climb", "paper", stop_bps=800.0, size_mult=1.5)
    assert base > 0
    assert abs(half - base * 0.5) < 1e-6
    assert more <= base * 1.5 + 1e-6 and more >= base       # caps may bind above 1.0
