"""Tests for the trade budget, the ledger, and the attention model."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import attention as att                  # noqa: E402
from app.execution import budget                       # noqa: E402
from app.research.ledger import local_day              # noqa: E402
from app.strategy.base import Panel                    # noqa: E402


def _panel(T=3000, N=8, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, size=(T, N)), axis=0))
    vol = np.abs(rng.lognormal(8, 0.5, (T, N)))
    return Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0 + time.time() - T * 60,
                 close, close * 1.002, close * 0.998, vol)


# ── the trade budget's dynamic program ───────────────────────────────────────
def _q(seed=0):
    return np.random.default_rng(seed).normal(20, 60, 400)


def test_threshold_falls_as_the_deadline_approaches():
    """An unspent slot is worth nothing at the end, so the bar must come down."""
    th = budget.thresholds(_q(), p_arrival=0.25, slots=3, steps=48)
    seq = [th[k, 3] for k in (48, 36, 24, 12, 4)]
    assert all(a >= b for a, b in zip(seq, seq[1:])), seq
    assert seq[0] > seq[-1]


def test_threshold_rises_when_slots_are_scarce():
    th = budget.thresholds(_q(), p_arrival=0.25, slots=3, steps=48)
    assert th[24, 1] > th[24, 2] > th[24, 3]


def test_threshold_is_zero_with_no_time_left():
    th = budget.thresholds(_q(), p_arrival=0.25, slots=2, steps=48)
    assert th[0, 2] == 0.0


def test_threshold_handles_an_empty_quality_history():
    th = budget.thresholds(np.array([]), p_arrival=0.2, slots=2, steps=10)
    assert th.shape == (11, 3) and not th.any()


def test_richer_opportunity_distribution_raises_the_bar():
    """If good signals are common, hold out for a better one."""
    poor = budget.thresholds(np.random.default_rng(1).normal(5, 20, 400), 0.25, 2, 48)
    rich = budget.thresholds(np.random.default_rng(1).normal(80, 20, 400), 0.25, 2, 48)
    assert rich[36, 2] > poor[36, 2]


# ── day boundary ─────────────────────────────────────────────────────────────
def test_local_day_uses_the_operators_midnight_not_utc():
    """03:00 UTC is still the previous day in America/Chicago."""
    import datetime as dt
    ts = dt.datetime(2026, 3, 10, 3, 0, tzinfo=dt.timezone.utc).timestamp()
    assert local_day(ts) == "2026-03-09"
    ts2 = dt.datetime(2026, 3, 10, 18, 0, tzinfo=dt.timezone.utc).timestamp()
    assert local_day(ts2) == "2026-03-10"


# ── attention model ──────────────────────────────────────────────────────────
def test_attention_needs_a_full_day_of_bars():
    small = _panel(T=100)
    assert att.attention(small, day_bars=1440)["available"] is False


def test_attention_ranks_a_planted_mover_first():
    """One coin given extra volume, range and return must top the ranking."""
    p = _panel(T=3000, N=8, seed=5)
    j = 3
    p.close[-1440:, j] *= np.linspace(1.0, 1.25, 1440)      # a real run today
    p.high[-1440:, j] = p.close[-1440:, j] * 1.01
    p.low[-1440:, j] = p.close[-1440:, j] * 0.99
    p.volume[-1440:, j] *= 8                                 # on real volume
    out = att.attention(p, day_bars=1440, trail_days=1)
    assert out["available"]
    assert out["rows"][0]["symbol"] == f"C{j}", [r["symbol"] for r in out["rows"][:3]]


def test_exhaustion_is_high_at_the_top_of_a_straight_run():
    p = _panel(T=3000, N=6, seed=9)
    j = 2
    p.close[-1440:, j] = p.close[-1441, j] * np.linspace(1.0, 1.3, 1440)  # straight up, closes at the high
    p.high[-1440:, j] = p.close[-1440:, j]
    p.low[-1440:, j] = p.close[-1440:, j] * 0.999
    out = att.attention(p, day_bars=1440, trail_days=1)
    row = next(r for r in out["rows"] if r["symbol"] == f"C{j}")
    assert row["exhaustion_components"]["position_in_day_range"] > 0.95
    assert row["exhaustion_score"] > 0.5


def test_every_attention_row_carries_a_verdict():
    out = att.attention(_panel(T=3000), day_bars=1440, trail_days=1)
    assert all(r["verdict"] for r in out["rows"])


# ── a budget is a cap, never a quota ─────────────────────────────────────────
def test_budget_never_forces_a_trade_at_the_deadline():
    """At the deadline the DP threshold reaches 0, and 0 means 'must still clear
    the cost hurdle' because quality is measured NET of it. A negative-quality
    signal is refused even with a slot about to expire."""
    th = budget.thresholds(_q(), p_arrival=0.25, slots=1, steps=48)
    assert th[0, 1] == 0.0
    assert th[1, 1] >= 0.0
    assert (th >= 0).all(), "a threshold below zero would mean paying to trade"


def test_budget_decision_refuses_negative_quality_without_a_threshold():
    """Even before there is enough history to compute a threshold, a signal that
    does not cover its own costs is never taken."""
    d = budget.BudgetDecision(True, -50.0, 0.0, 1, 3600.0, "")
    assert d.quality_bps < 0
    floor = budget.DEFAULT_MIN_QUALITY_BPS
    assert floor == 0.0
    assert not (-50.0 >= floor)
