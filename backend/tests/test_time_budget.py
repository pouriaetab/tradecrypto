"""The clock is predicted, not hardcoded (2026-09-23).

The operator: "the clock for all it doesnt have to be hard coded ... if position
within certain predicted interval didnt reach the point the algorithm predicted
that it would then to consider it as not a good trade."
"""
import pytest

from app.strategy.time_budget import (RATIO_TABLE, budget, is_late, worth_taking)


def test_a_harder_target_takes_longer_and_lands_less_often():
    """THE WHOLE RELATIONSHIP. If this inverts, the table was mis-entered and
    every window in production is wrong in the dangerous direction."""
    prev_p, prev_h = 2.0, -1
    for lo, p, p50, p70 in RATIO_TABLE:
        assert p < prev_p, f"reach rate did not fall at ratio {lo}"
        assert p70 > prev_h, f"the window did not lengthen at ratio {lo}"
        prev_p, prev_h = p, p70


def test_the_window_is_driven_by_the_coin_not_by_a_constant():
    """Same 4% target, two coins. The quiet one must get a longer leash."""
    fast = budget(0.04, 0.030)       # 3% hourly range
    slow = budget(0.04, 0.005)       # 0.5% hourly range
    assert fast["p70_h"] < slow["p70_h"]
    assert fast["p_reach"] > slow["p_reach"]
    assert fast["p70_h"] == 12 and slow["p70_h"] == 35


def test_a_coin_with_no_range_is_treated_as_the_worst_case():
    """Missing bars must not silently produce a generous window."""
    for bad in (0.0, None, -1.0):
        b = budget(0.04, bad)
        assert b["p_reach"] == RATIO_TABLE[-1][1]
        assert b["p70_h"] == RATIO_TABLE[-1][3]
        assert b["ratio"] is None


def test_is_late_uses_the_predicted_window():
    assert not is_late(0.04, 0.030, 11.0)
    assert is_late(0.04, 0.030, 13.0)
    assert not is_late(0.04, 0.005, 13.0), "the quiet coin is not late at 13h"


def test_worth_taking_refuses_the_shapes_that_rarely_arrive():
    assert worth_taking(0.04, 0.030)          # 75% arrive
    assert not worth_taking(0.04, 0.005)      # 18% arrive


def test_the_engine_uses_the_predicted_window_and_not_a_constant():
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index('eff_target = dynamic_target(')
    window = src[max(0, i - 1500):i + 400]
    # Re-pointed 2026-09-23: the decay now starts at a fraction of the MEDIAN
    # hour, not the full window -- a good gain was going unclaimed while the
    # system waited for a target that probably was not coming. The INVARIANT is
    # unchanged: the hour comes from this coin's own measured budget, never from
    # a constant.
    # Re-pointed 2026-09-23 for the third time, and now at the right invariant:
    # the target must come down on the ODDS, not on any hour at all. The two
    # earlier versions of this test both asserted a clock, which is what the
    # operator kept objecting to.
    assert "shrink=_shrink" in window, "the target is not driven by the odds"
    assert "decay_after_h=" not in window, "a clock is back in the decision"


def test_a_late_trade_is_recorded_once_for_feedback():
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    assert "_note_late_trade" in src
    assert "_LATE_NOTED" in src, "a late trade would be logged on every tick"
    # the key must include the position's identity, not just the symbol: two
    # strategies can hold the same coin, and both deserve their own note
    i = src.index("_LATE_NOTED.add")
    key = src[max(0, i - 400):i]
    for part in ('pos["symbol"]', 'pos["strategy"]', 'pos["opened_ts"]'):
        assert part in key, f"the dedupe key ignores {part}"
    assert "trade_late" in inspect.getsource(engine._note_late_trade)


def test_the_decay_starts_before_the_median_hour():
    """Waiting for the full window let a +4.9% gain on UNI go unclaimed while a
    9.9% target that arrives 48% of the time never came. Swept on 59 trades:
    volatility falls monotonically the earlier the decay starts (4.83% at never
    -> 3.91% at 0.5x median) and trades losing more than 5% fall from 20% to
    14%."""
    from app.execution import engine
    assert 0.2 <= engine.TARGET_DECAY_AT_MEDIAN_K <= 1.0, (
        "past 1.0x the median the risk reduction is gone; below 0.2x the target "
        "is gutted before the trade has had any chance")


def test_the_two_early_exits_are_not_both_on():
    """Measured: the recovery exit and the target decay do the same job. With
    the decay on, adding recovery moved the mean from +0.072% to -0.361% while
    the risk numbers barely moved. Running both takes the same small profit
    twice as often and costs money."""
    from app.execution import engine
    assert not (engine.RECOVERY_CLOSES and engine.TARGET_DECAY_AT_MEDIAN_K <= 1.0), \
        "both early-exit mechanisms are live; they are substitutes, not additions"


def test_the_target_comes_down_on_the_odds_not_the_clock():
    """The operator, three times: "dont do the hour or timely hardcoding".

    `still_arrives` sees where the price IS, which no clock can. These two cases
    are the whole argument -- the clock gets both backwards."""
    from app.strategy.time_budget import still_arrives
    broken_early = still_arrives(0.04, 0.02, 2.0, -5.5)     # 2h in, already down
    old_but_working = still_arrives(0.04, 0.02, 32.0, 2.5)  # 32h in, up
    assert broken_early < 0.40, "a trade that broke at 2h is not flagged"
    assert old_but_working > 0.40, "a 32h trade that is working would be cut"
    assert old_but_working > broken_early * 1.5


def test_the_odds_fall_as_a_trade_goes_wrong():
    from app.strategy.time_budget import still_arrives
    prev = 1.0
    for pnl in (3.0, 1.0, -1.0, -3.0, -6.0):
        p = still_arrives(0.04, 0.02, 8.0, pnl)
        assert p <= prev, f"odds did not fall at {pnl}%"
        prev = p


def test_the_shrink_is_per_hour_not_per_tick():
    """The engine ticks every few seconds. A per-tick accumulator decays about
    sixty times too fast in production while looking right in an hourly
    backtest."""
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine._sluggish_shrink)
    assert "3600" in src, "the shrink is not normalised to hours"
    assert "+=" not in src, "the shrink accumulates per call instead of per hour"


def test_the_moment_the_prediction_turned_is_recorded_for_the_model():
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine._sluggish_shrink)
    assert "prediction_turned" in src, "no penalty feedback is written"
    for f in ("odds_now", "age_h", "strategy"):
        assert f in src, f"the feedback row drops {f}"
