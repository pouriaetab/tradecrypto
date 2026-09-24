"""The stop's distance, and whether a trade shape can pay for its own spread."""
import math

from app.strategy._stops import (conviction_stop_bps, breakeven_win_rate,
                                 shape_survives_costs)


def test_floor_target_leaves_the_stop_exactly_where_it_was():
    """The ordinary trade must be untouched, or this is a rewrite pretending
    to be an adjustment."""
    assert conviction_stop_bps(800.0, 400.0, 400.0) == 800.0


def test_bigger_target_widens_the_stop_and_smaller_tightens_it():
    tight = conviction_stop_bps(800.0, 320.0, 400.0)
    wide = conviction_stop_bps(800.0, 580.0, 400.0)
    assert tight < 800.0 < wide


def test_the_move_is_damped_not_proportional():
    """A full ratio-preserving move would put a 7.5% target on a 15% stop.
    The operator asked for slow; slow has to be provable."""
    full = 800.0 * (750.0 / 400.0)
    got = conviction_stop_bps(800.0, 750.0, 400.0)
    assert got < full / 1.4


def test_never_moves_more_than_a_quarter_from_base():
    for tgt in (1.0, 50.0, 400.0, 5000.0, 1e9):
        out = conviction_stop_bps(800.0, tgt, 400.0)
        assert 600.0 - 1e-9 <= out <= 1000.0 + 1e-9, tgt


def test_bad_inputs_return_the_base_stop_untouched():
    for bad in (None, 0.0, -5.0, float("nan"), "x"):
        assert conviction_stop_bps(800.0, bad, 400.0) == 800.0
        assert conviction_stop_bps(800.0, 400.0, bad) == 800.0


def test_breakeven_matches_the_burst_catch_arithmetic():
    """4% target, 3% stop, 1.9% round trip -> 70%. This is the number that
    condemned burst_catch; if it drifts, the diagnosis drifts with it."""
    be = breakeven_win_rate(400.0, 300.0, 190.0)
    assert math.isclose(be, 0.70, abs_tol=0.005)


def test_lowering_the_target_makes_the_shape_worse_not_better():
    """The operator's instinct was to cut the target. The toll is charged per
    round trip, so a smaller target is a larger share lost. Encoded here
    because it is counter-intuitive and will be proposed again."""
    assert breakeven_win_rate(250.0, 300.0, 190.0) > breakeven_win_rate(400.0, 300.0, 190.0)


def test_target_below_the_round_trip_cannot_win():
    assert breakeven_win_rate(150.0, 300.0, 190.0) is None
    ok, why, be = shape_survives_costs(150.0, 300.0, 190.0)
    assert not ok and be is None and "cannot win even when it is right" in why


def test_burst_catch_as_configured_is_refused():
    ok, why, be = shape_survives_costs(400.0, 300.0, 190.0)
    assert not ok
    assert math.isclose(be, 0.70, abs_tol=0.005)
    assert "70%" in why


def test_a_workable_shape_passes():
    ok, why, be = shape_survives_costs(700.0, 300.0, 190.0)
    assert ok and why == "" and be < 0.65


def test_free_venue_never_blocks():
    ok, _, _ = shape_survives_costs(400.0, 300.0, 0.0)
    assert ok


def test_burst_catch_consults_the_gate():
    import inspect
    from app.strategy.burst_catch import BurstCatch
    src = inspect.getsource(BurstCatch)
    assert "shape_survives_costs" in src, "burst_catch no longer checks its own costs"
