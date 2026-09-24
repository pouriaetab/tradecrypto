"""A strategy parked in the lab must keep being studied, or "kept for training"
is just a nicer word for deleted.

2026-09-22. burst_catch was retired: off ACTIVE_STRATEGIES, trades moved to
mode='lab' so the book stops counting them and exit_lab keeps reading them. The
plan -- and the operator's understanding -- was that it would go on being
studied daily so it could be tuned and earn its way back.

It was not. Coming off ACTIVE_STRATEGIES silently stopped SIX daily research
paths from looking at it: the entry-quality fit, the retrain check, the per-day
strategy replay, the regime scorecard, the version ledger and the relearn
cards. Only exit_lab still saw it, because exit_lab consults no roster at all.
Nothing reported the gap; the operator had to remember and ask.
"""
import inspect

import pytest

from app.core import db
from app.strategy import registry


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def _lab_trade(strategy):
    db.execute(
        """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px,
                              ts_open, ts_close, holding_s, gross_pnl_usd,
                              cost_usd, net_pnl_usd)
           VALUES ('C',?, 'lab',1.0,10.0,9.0,1.0,2.0,60.0,-0.5,0.5,-1.0)""",
        (strategy,))


def test_a_retired_strategy_with_lab_trades_is_still_studied():
    """THE REGRESSION."""
    assert "burst_catch" not in registry.ACTIVE_STRATEGIES
    assert "burst_catch" not in registry.studied()
    _lab_trade("burst_catch")
    assert "burst_catch" in registry.studied(), \
        "a strategy parked in the lab is not being studied — it is frozen"


def test_studied_never_drops_an_active_strategy():
    for s in registry.ACTIVE_STRATEGIES:
        assert s in registry.studied()


def test_studied_does_not_invent_strategies():
    """A row for a rule the code no longer has must not become a roster entry."""
    _lab_trade("a_rule_that_no_longer_exists")
    assert "a_rule_that_no_longer_exists" not in registry.studied()


def test_studied_falls_back_to_active_if_the_database_is_unreachable(monkeypatch):
    """A research job that cannot reach the database should study the live
    strategies, not none of them."""
    def boom(*a, **k):
        raise RuntimeError("no database")
    monkeypatch.setattr(db, "query", boom)
    assert registry.studied() == list(registry.ACTIVE_STRATEGIES)


def test_a_retired_strategy_still_may_not_trade():
    """The other half. studied() is about learning; it must never widen who can
    open a position."""
    _lab_trade("burst_catch")
    from app.execution import engine
    src = inspect.getsource(engine._roster if hasattr(engine, "_roster") else engine)
    assert "ACTIVE_STRATEGIES" in src, \
        "the execution roster no longer reads ACTIVE_STRATEGIES"
    assert "studied(" not in src, \
        "the execution path is using the STUDY roster — a retired strategy could trade"


RESEARCH_MODULES = [
    "app.research.entry_quality",
    "app.research.retrain",
    "app.research.strategy_days",
    "app.research.regime_days",
    "app.research.versions",
    "app.feedback.relearn",
]


@pytest.mark.parametrize("modname", RESEARCH_MODULES)
def test_every_research_path_uses_the_study_roster(modname):
    """The failure that happened was six correct modules asking the wrong list.
    A unit test of studied() would not have caught it; this does."""
    import importlib
    src = inspect.getsource(importlib.import_module(modname))
    assert "ACTIVE_STRATEGIES" not in src, (
        f"{modname} still iterates ACTIVE_STRATEGIES — a retired strategy in the "
        f"lab is invisible to it")


def test_the_scheduler_research_jobs_use_the_study_roster():
    from app.core import scheduler
    src = inspect.getsource(scheduler)
    assert "ACTIVE_STRATEGIES" not in src, \
        "a scheduled research job is back on the trading roster"
