"""Several models per strategy, and the discipline that makes the numbers mean
anything.

The operator asked for model competition: variables competing for influence,
more than one model per strategy, judged on AUC and Brier, with a train and
validate split. These tests cover the four ways that goes wrong quietly, each of
which produces a confident number that is worth nothing:

  1. Splitting at random instead of by time. Shuffling a price series lets a
     model learn from an afternoon to predict that morning; every score after
     that is inflated and nothing about the output looks wrong.
  2. No control in the field. Without a variable-free entrant there is nothing
     to beat, so the best of five bad models reads as a winner.
  3. Crowning on a point estimate. AUC 0.79 on eleven held-out trades has a
     confidence interval from 0.40 to 1.00 — that is not a champion.
  4. Fitting at all on too few outcomes, which describes one fortnight and
     calls it a model.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from app.research import model_arena as arena


def test_the_split_is_by_time_and_never_shuffled():
    """The single most expensive mistake available here."""
    src = inspect.getsource(arena.run)
    assert "X[:cut]" in src and "X[cut:]" in src, (
        "the split must slice a time-ordered array, not sample it"
    )
    # MATCH CALLS, NOT PROSE. The first version searched for the bare word
    # "shuffle" and failed on this module's own sentence explaining why it does
    # not shuffle. A test that fires on its subject's documentation is noise,
    # and it is the second time in this codebase a check has read a comment as
    # code.
    body = src.split('"""', 2)[-1]          # drop the docstring
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    for bad in ("shuffle(", "permutation(", "train_test_split(", "random.sample("):
        assert bad not in code, f"{bad} in the split destroys the time ordering"
    rows_src = inspect.getsource(arena._rows)
    assert "ORDER BY s.ts ASC" in rows_src, "rows must come out in time order"


def test_a_control_with_no_variables_is_always_in_the_field():
    """Without something to beat, the best of five bad models looks like a win."""
    src = inspect.getsource(arena._entrants)
    assert '"base_rate"' in src
    assert "THE CONTROL" in src


def test_the_control_alone_cannot_be_a_champion(monkeypatch):
    """A constant predictor has AUC 0.5 by construction, so its interval can
    never clear 0.5 — but assert it, because a champion that is the control
    would mean the ranking logic had inverted somewhere."""
    y = np.array([1.0, 0.0] * 12)
    p = np.full(len(y), 0.5)
    sc = arena._score(y, p)
    assert sc["auc"] == pytest.approx(0.5, abs=1e-9)
    assert sc["beats_chance"] is False


def test_nothing_is_fitted_below_the_floor(writable_db):
    """A model fitted on a dozen trades describes a fortnight, not a strategy."""
    from app.core import db
    db.execute("DELETE FROM signals")
    out = arena.run("day_climb")
    assert out["available"] is False
    assert out["n_outcomes"] < arena.MIN_TRADES
    assert str(arena.MIN_TRADES) in out["why"]


def test_a_champion_must_clear_chance_on_its_interval_not_its_point():
    """The rule that stops a lucky eleven-trade holdout being called a model."""
    src = inspect.getsource(arena.run)
    assert 'beats_chance' in src, "the champion filter must use the interval"
    assert 'winners[0] if winners else None' in src
    # and the interval itself must be resampled, not assumed
    assert "percentile" in inspect.getsource(arena._auc_ci)


def test_thinness_is_reported_rather_than_hidden():
    """Ten variables on forty outcomes is fittable and not yet trustworthy.
    Both facts have to reach the screen."""
    src = inspect.getsource(arena.run)
    assert "thin" in src
    assert "ROWS_PER_FEATURE" in inspect.getsource(arena) or "want =" in src


def test_it_never_claims_to_steer_trading():
    """This is evidence for changing the live ranking, not the change.
    If that ever stops being true, this test should be the thing that fails."""
    src = inspect.getsource(arena.run)
    assert '"applies_to_trading": False' in src


def test_only_variables_present_on_nearly_every_row_are_used():
    """A column missing half the time is imputed noise, and imputed noise is
    what lets a wide model look clever on the training slice."""
    assert "0.95 * len(feats)" in inspect.getsource(arena._matrix)


def test_bookkeeping_fields_are_not_treated_as_predictors():
    """`demo` is stamped on every demo row. Left in, a model would discover that
    demo trades differ from live ones and score beautifully on nothing."""
    assert "demo" in arena._SKIP
    assert "burst_rank" in arena._SKIP
