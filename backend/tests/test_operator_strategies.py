"""Tests for the four strategies the operator described, and their machinery."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import regime as regime_mod                       # noqa: E402
from app.research.seasonality import benjamini_hochberg         # noqa: E402
from app.strategy._fitting import fit_slope                     # noqa: E402
from app.strategy.base import Panel                             # noqa: E402
from app.strategy.forced_momentum import efficiency_ratio, run_length  # noqa: E402
from app.strategy.registry import OPERATOR_STRATEGIES, build, grid     # noqa: E402


def _panel(T=1200, N=8, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    lr = rng.normal(drift, 0.002, size=(T, N))
    close = 100 * np.exp(np.cumsum(lr, axis=0))
    vol = np.abs(rng.lognormal(8, 0.5, (T, N)))
    return Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0 + time.time() - T * 60,
                 close, close * 1.001, close * 0.999, vol)


# ── the uniform "no edge claimed" rule ───────────────────────────────────────
@pytest.mark.parametrize("name", OPERATOR_STRATEGIES)
def test_uncalibrated_strategy_claims_no_edge(name):
    """Before calibration every strategy must report zero expected edge, so the
    cost hurdle blocks it. A strategy that trades on its defaults is a bug."""
    s = build(name)
    p = _panel()
    sigs = s.generate(p, p.T - 1)
    assert all(sig.expected_edge_bps == 0.0 for sig in sigs)


@pytest.mark.parametrize("name", OPERATOR_STRATEGIES)
def test_calibration_on_noise_claims_no_edge(name):
    """On a driftless random walk there is nothing to find, and each strategy
    must say so rather than fitting noise."""
    s = build(name)
    p = _panel(T=1500, seed=3)
    s.calibrate(p, p.T // 2)
    claimed = (s.params.get("beta_bps_per_unit", 0.0) or 0.0)
    assert claimed == 0.0, f"{name} claimed an edge on pure noise: {s.params.get('calib_note')}"


@pytest.mark.parametrize("name", OPERATOR_STRATEGIES)
def test_every_operator_strategy_has_a_grid(name):
    g = grid(name)
    assert g, f"{name} has no parameter grid, so its deflated Sharpe cannot be honest"
    n = 1
    for v in g.values():
        n *= len(v)
    assert n >= 4


# ── fitting rule ─────────────────────────────────────────────────────────────
def test_fit_slope_zeroes_an_insignificant_slope():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 500)
    y = rng.normal(0, 100, 500)              # no relationship
    assert fit_slope(x, y)["beta"] == 0.0


def test_fit_slope_recovers_a_real_slope():
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1, 2000)
    y = 25 * x + rng.normal(0, 40, 2000)
    f = fit_slope(x, y)
    assert f["significant"] and 20 < f["beta"] < 30


def test_fit_slope_refuses_small_samples():
    assert fit_slope(np.arange(50.0), np.arange(50.0) * 10)["beta"] == 0.0


# ── efficiency ratio ─────────────────────────────────────────────────────────
def test_efficiency_ratio_is_one_for_a_straight_line():
    close = np.linspace(100, 110, 60).reshape(-1, 1)
    er = efficiency_ratio(close, 59, 30)
    assert er[0] == pytest.approx(1.0, abs=1e-6)


def test_efficiency_ratio_is_near_zero_for_a_round_trip():
    """Up ten then back down ten: maximum path, zero net travel -> ER ~ 0.

    The window must span the whole round trip; a window that starts mid-run
    measures a partial move and correctly returns something non-zero.
    """
    up = np.linspace(100, 110, 30)
    down = np.linspace(110, 100, 31)[1:]
    close = np.concatenate([up, down]).reshape(-1, 1)
    last = close.shape[0] - 1
    er = efficiency_ratio(close, last, last)          # full span
    assert abs(er[0]) < 0.01


def test_run_length_counts_consecutive_higher_closes():
    close = np.array([1, 2, 3, 4, 5.0]).reshape(-1, 1)
    assert run_length(close, 4)[0] == 4
    close2 = np.array([1, 2, 3, 2, 3.0]).reshape(-1, 1)
    assert run_length(close2, 4)[0] == 1


# ── multiple-testing correction ──────────────────────────────────────────────
def test_bh_rejects_nothing_on_uniform_pvalues():
    rng = np.random.default_rng(4)
    p = rng.uniform(0, 1, 200)
    assert benjamini_hochberg(p, 0.10).sum() <= 5      # a handful at most, by construction


def test_bh_finds_a_planted_effect():
    rng = np.random.default_rng(5)
    p = np.concatenate([rng.uniform(0, 1, 100), np.full(5, 1e-6)])
    assert benjamini_hochberg(p, 0.10)[-5:].all()


# ── regime ───────────────────────────────────────────────────────────────────
def test_breadth_is_high_in_an_uptrend_and_low_in_a_downtrend():
    up = _panel(T=600, drift=0.0012, seed=6)
    down = _panel(T=600, drift=-0.0012, seed=6)
    b_up = regime_mod.breadth(up, up.T - 1, 120)["value"]
    b_dn = regime_mod.breadth(down, down.T - 1, 120)["value"]
    assert b_up > 0.8 and b_dn < 0.2


def test_regime_reports_unavailable_without_history():
    tiny = _panel(T=5, N=3)
    assert regime_mod.regime_report(tiny)["available"] is False


def test_flow_proxy_is_labelled_as_a_proxy():
    p = _panel(T=300)
    f = regime_mod.flow_pressure(p, p.T - 1)
    assert f["available"] and "NOT whale detection" in f["caveat"]
