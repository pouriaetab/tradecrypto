"""Property tests for the parts whose failure would be silent and expensive."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research import stats as S                      # noqa: E402
from app.strategy.base import Panel, robust_z            # noqa: E402
from app.strategy.top_mover_reversal import TopMoverReversal  # noqa: E402
from app.research import backtest as bt                  # noqa: E402


def test_deflated_sharpe_rejects_noise():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, 500)
    assert S.deflated_sharpe(noise, n_trials=100)["deflated_sharpe"] < 0.5


def test_pbo_near_half_on_noise_and_low_with_real_edge():
    rng = np.random.default_rng(1)
    X = rng.normal(0, 0.01, size=(500, 16))
    assert 0.2 < S.pbo_cscv(X)["pbo"] < 0.8
    X2 = X.copy()
    X2[:, 5] += 0.003
    assert S.pbo_cscv(X2)["pbo"] < 0.25


def test_sizing_refuses_when_edge_ci_includes_zero():
    assert S.kelly_with_uncertainty(-0.0001, 0.0004) == 0.0
    assert S.kelly_with_uncertainty(0.0, 0.0004) == 0.0
    assert S.kelly_with_uncertainty(0.002, 0.0004) > 0


def test_estimate_reports_insufficient_below_min_n():
    e = S.Estimate(value=1.0, n=5, min_n=30)
    assert not e.sufficient
    assert S.Estimate(value=1.0, n=50, min_n=30).sufficient


def test_robust_z_is_not_dominated_by_one_outlier():
    x = np.array([0.0, 0.01, -0.01, 0.005, -0.005, 3.0])
    z = robust_z(x)
    assert abs(z[0]) < 1.0            # the ordinary points stay ordinary
    assert z[-1] > 5                  # the outlier is still flagged


def test_posterior_moves_toward_evidence():
    rng = np.random.default_rng(2)
    post = S.NormalInverseGamma().update(rng.normal(0.002, 0.005, 400))
    assert 0.0015 < post.mean < 0.0025
    assert post.prob_above(0.0) > 0.99
    assert post.prob_above(0.02) < 0.01


def test_backtest_has_no_lookahead_on_pure_noise():
    """On a random walk with zero cost, mean net return must be indistinguishable
    from zero. If it is reliably positive, the backtester is cheating."""
    rng = np.random.default_rng(5)
    T, N = 2500, 15
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, size=(T, N)), axis=0))
    panel = Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0, close, close, close)
    s = TopMoverReversal(lookback_bars=20, hold_bars=10, z_enter=1.5,
                         vol_window=30, max_vol_bps=1000)
    s.params["beta_bps_per_z"] = 10.0        # force it to trade
    r = bt.run_backtest(panel, s, cost_bps_per_side=0.0, apply_hurdle=False)
    if r.net_returns.size >= 30:
        _, lo, hi = S.bootstrap_ci(r.net_returns, np.mean, n_boot=1500)
        assert lo < 0 < hi, "zero-cost noise backtest should not be significantly profitable"


def test_costs_always_reduce_returns():
    rng = np.random.default_rng(6)
    T, N = 2000, 12
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, size=(T, N)), axis=0))
    panel = Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0, close, close, close)
    s = TopMoverReversal(lookback_bars=20, hold_bars=10, z_enter=1.2, max_vol_bps=1000)
    s.params["beta_bps_per_z"] = 10.0
    free = bt.run_backtest(panel, s, cost_bps_per_side=0.0, apply_hurdle=False)
    paid = bt.run_backtest(panel, s, cost_bps_per_side=50.0, apply_hurdle=False)
    if free.net_returns.size > 5 and paid.net_returns.size > 5:
        assert paid.net_returns.mean() < free.net_returns.mean()


def test_short_signals_are_dropped_when_long_only():
    rng = np.random.default_rng(7)
    T, N = 1500, 12
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, size=(T, N)), axis=0))
    panel = Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0, close, close, close)
    s = TopMoverReversal(lookback_bars=20, hold_bars=10, z_enter=1.2, max_vol_bps=1000)
    s.params["beta_bps_per_z"] = 10.0
    r = bt.run_backtest(panel, s, cost_bps_per_side=0.0, long_only=True, apply_hurdle=False)
    assert r.dropped_short_signals > 0
    assert all(t.side == "buy" for t in r.trades)
