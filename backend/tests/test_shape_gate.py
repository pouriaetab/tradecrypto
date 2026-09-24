"""The entry gate refuses a target shape that rarely arrives (2026-09-23).

The operator: "only promoted when the chances are high to profit."

It is deliberately set LOW. On the 69 closed trades with a recoverable target a
gate at 30% would have refused nothing, at 40% two trades worth -$0.67, and at
50% fourteen trades worth +$7.97 -- it would have destroyed the book. This is a
rail against a shape the desk has not produced yet, not an optimiser.
"""
import pytest

from app.risk import guards


def test_the_threshold_stays_in_the_range_the_evidence_supports():
    assert 0.25 <= guards.MIN_P_REACH <= 0.40, (
        "a gate above 0.40 is contradicted by the backtest: at 50% it refuses "
        "14 trades worth +$7.97")


def test_a_strategy_without_a_target_is_not_judged_on_one(writable_db):
    d = guards.pre_trade_check(symbol="BTC", side="buy", notional_usd=50.0,
                               mode="paper", strategy="pump_ride", target_frac=None)
    assert not any(c["check"] == "shape_odds" for c in d.checks), \
        "a no-target strategy was graded on a target it does not have"


def test_an_impossible_shape_is_refused(writable_db):
    from app.core import db
    import time
    now = time.time()
    for i in range(24):          # a very quiet coin: 0.2% hourly range
        px = 100.0
        db.execute("INSERT INTO bars(symbol,granularity,ts,open,high,low,close,volume,source) "
                   "VALUES ('QUIET',3600,?,?,?,?,?,1,'test')",
                   (now - (24 - i) * 3600, px, px * 1.001, px * 0.999, px))
    d = guards.pre_trade_check(symbol="QUIET", side="buy", notional_usd=50.0,
                               mode="paper", strategy="day_climb", target_frac=0.10)
    shape = next(c for c in d.checks if c["check"] == "shape_odds")
    assert not shape["passed"]
    assert "typical hourly move" in shape["detail"], \
        "the refusal does not say WHY in plain language"


def test_a_reasonable_shape_passes(writable_db):
    from app.core import db
    import time
    now = time.time()
    for i in range(24):          # 3% hourly range
        px = 100.0
        db.execute("INSERT INTO bars(symbol,granularity,ts,open,high,low,close,volume,source) "
                   "VALUES ('LIVELY',3600,?,?,?,?,?,1,'test')",
                   (now - (24 - i) * 3600, px, px * 1.015, px * 0.985, px))
    d = guards.pre_trade_check(symbol="LIVELY", side="buy", notional_usd=50.0,
                               mode="paper", strategy="day_climb", target_frac=0.04)
    shape = next(c for c in d.checks if c["check"] == "shape_odds")
    assert shape["passed"]


def test_the_engine_hands_the_gate_the_target(writable_db):
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index("guards.pre_trade_check(")
    call = src[i:src.index(")\n", src.index("target_frac=", i))]
    assert "target_frac=" in call, "the gate can never fire — it is never given a target"


def test_a_lone_candidate_is_refused(writable_db):
    """Measured: bars with exactly one qualifying coin returned -3.11%/trade
    against +0.57% when two or more did (14 vs 58 trades, p=0.007)."""
    d = guards.pre_trade_check(symbol="BTC", side="buy", notional_usd=50.0,
                               mode="paper", strategy="day_climb", field_size=1)
    c = next(x for x in d.checks if x["check"] == "lone_candidate")
    assert not c["passed"]
    assert "only coin" in c["detail"].lower()
    assert c["actual"] == 1


def test_a_crowded_field_passes_that_check(writable_db):
    d = guards.pre_trade_check(symbol="BTC", side="buy", notional_usd=50.0,
                               mode="paper", strategy="day_climb", field_size=4)
    c = next(x for x in d.checks if x["check"] == "lone_candidate")
    assert c["passed"]


def test_an_unknown_field_size_does_not_silently_block(writable_db):
    """A caller that does not pass field_size must not have every entry refused."""
    d = guards.pre_trade_check(symbol="BTC", side="buy", notional_usd=50.0,
                               mode="paper", strategy="day_climb")
    assert not any(x["check"] == "lone_candidate" for x in d.checks)


def test_the_engine_tells_the_gate_how_many_competed(writable_db):
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index("guards.pre_trade_check(")
    call = src[i:src.index(")\n", src.index("target_frac=", i))]
    assert "field_size=" in call, "the lone-candidate check can never fire"


def test_every_gate_is_recorded_on_the_signal_not_just_the_first_failure(writable_db):
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    assert "UPDATE signals SET checks_json=?" in src
    i = src.index("UPDATE signals SET checks_json=?")
    blk = src[i:i + 600]
    for field in ('"check"', '"passed"', '"limit"', '"actual"'):
        assert field in blk, f"the stored chain drops {field}"
