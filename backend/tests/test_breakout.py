"""Tests for the false-breakout system.

The most important test here is the look-ahead one: if the feature builder can
see the future, every metric in the Breakout page is a lie.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research import breakout as bo          # noqa: E402
from app.strategy.base import Panel              # noqa: E402


def _panel(T=3000, N=4, seed=0):
    r = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(r.normal(0, 0.002, size=(T, N)), axis=0))
    nz = np.abs(r.normal(0, 0.0012, (T, N)))
    vol = np.abs(r.lognormal(8, 0.5, (T, N)))
    return Panel([f"C{i}" for i in range(N)], np.arange(T) * 60.0 + time.time() - T * 60,
                 close, close * (1 + nz), close * (1 - nz), vol)


# ── no look-ahead ────────────────────────────────────────────────────────────
def test_features_do_not_depend_on_the_future():
    """Compute a feature vector, then scribble over every bar after t and compute
    it again. Identical output is the only acceptable result."""
    p = _panel(T=2000, seed=1)
    j = 0
    ctx = bo.symbol_context(p, j)
    t = 1200
    lv = bo.Level(price=float(p.close[t, j] * 0.99), kind="pivot_high",
                  first_idx=t - 200, last_touch_idx=t - 50)
    a = ctx["atr60"]
    bo._LEVEL_CACHE[("C0", t)] = [lv]
    f1 = bo._feature_vector(j, "C0", t, lv, 1, p, a, None, ctx)

    q = _panel(T=2000, seed=1)
    rng = np.random.default_rng(99)
    q.close[t + 1:] *= np.exp(rng.normal(0, 0.05, q.close[t + 1:].shape))
    q.high[t + 1:] = q.close[t + 1:] * 1.05
    q.low[t + 1:] = q.close[t + 1:] * 0.95
    q.volume[t + 1:] *= 50
    ctx2 = bo.symbol_context(q, j)
    bo._LEVEL_CACHE[("C0", t)] = [lv]
    f2 = bo._feature_vector(j, "C0", t, lv, 1, q, ctx2["atr60"], None, ctx2)

    for k in bo.FEATURE_NAMES:
        assert f1[k] == pytest.approx(f2[k], rel=1e-9, abs=1e-9), (
            f"feature {k!r} changed when only FUTURE bars changed — look-ahead bug")


def test_trailing_mean_never_uses_the_next_bar():
    x = np.array([1.0, 1.0, 1.0, 100.0, 1.0])
    m = bo._trailing_mean(x, 3)
    assert m[2] == pytest.approx(1.0)          # would be ~34 if it peeked at x[3]
    assert m[3] == pytest.approx((1 + 1 + 100) / 3)


def test_pivot_levels_respect_their_own_lag():
    """A pivot needs `right` bars after it, so a level must never be usable before
    those bars exist."""
    p = _panel(T=600, seed=2)
    close, high, low = p.close[:, 0], p.high[:, 0], p.low[:, 0]
    vol = p.volume[:, 0]
    t = 300
    lv = bo.levels_as_of(high, low, close, vol, t, lookback=280, pivot_right=5)
    for l in lv:
        if l.kind.startswith("pivot"):
            assert l.first_idx + 5 <= t, f"pivot at {l.first_idx} used too early at t={t}"


# ── level machinery ──────────────────────────────────────────────────────────
def test_round_levels_are_actually_round():
    lv = bo.round_levels(0.2456)
    prices = [l.price for l in lv]
    assert any(abs(p - 0.25) < 1e-9 for p in prices)
    assert all(p > 0 for p in prices)


def test_clustering_merges_nearby_levels_and_adds_touches():
    lv = [bo.Level(100.0, "pivot_high"), bo.Level(100.05, "round"),
          bo.Level(120.0, "pivot_high")]
    merged = bo.cluster_levels(lv, tol=0.5)
    assert len(merged) == 2
    assert merged[0].touches == 2


# ── labelling ────────────────────────────────────────────────────────────────
def test_labels_are_binary_and_explained():
    p = _panel(T=3000, seed=3)
    evs = bo.find_breakouts(p, "C0")
    assert evs, "expected at least one breakout on 3000 bars"
    assert all(e.label in (0, 1) for e in evs)
    assert all(e.label_reason for e in evs)


# ── the model ────────────────────────────────────────────────────────────────
def test_logistic_recovers_a_planted_relationship():
    rng = np.random.default_rng(4)
    n = 3000
    X = rng.normal(0, 1, (n, 3))
    logit = 0.5 + 1.5 * X[:, 0] - 1.0 * X[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-logit))).astype(float)
    m = bo.fit_logistic(X, y, ["a", "b", "c"])
    coefs = {c["feature"]: c["coef"] for c in m.coefficients()}
    assert coefs["a"] > 0.8 and coefs["b"] < -0.5
    assert abs(coefs["c"]) < 0.3


def test_auc_is_half_on_random_scores_and_one_on_perfect():
    y = np.array([0, 0, 1, 1.0])
    assert bo.auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert bo.auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)
    assert bo.auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == pytest.approx(0.5)


def test_model_refuses_to_exist_without_enough_events():
    p = _panel(T=400, N=2, seed=5)
    rep = bo.train(p)
    assert rep["available"] is False and "60" in str(rep.get("n_events_required", 60))


def test_veto_blocks_nothing_without_a_proven_model():
    bo._MODEL.update({"model": None, "report": None})
    p = _panel(T=600, seed=6)
    v = bo.veto(p, "C0")
    assert v["block"] is False and v["state"] == "no model"


def test_veto_blocks_nothing_when_the_model_has_no_skill():
    bo._MODEL.update({"model": object(), "report": {
        "available": True, "out_of_sample": {"beats_chance_at_95": False}}})
    p = _panel(T=600, seed=7)
    v = bo.veto(p, "C0")
    assert v["block"] is False and v["state"] == "no skill"
    bo._MODEL.update({"model": None, "report": None})
