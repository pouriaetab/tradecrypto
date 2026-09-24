"""The dynamic target: there is always a target, and it re-prices as the trade
runs (2026-09-23, the operator's request).

Only the LOWERING half is live. The raising half -- holding the target above the
price while the coin climbs -- was replayed on all 85 closed paper trades and
changed zero outcomes, so it stays in the lab. These tests pin the two
properties that make the live half safe.
"""
import pytest

from app.strategy.dynamic_target import dynamic_target


ENTRY, COVERED, BASE = 100.0, 100.96, 0.0692


def test_there_is_always_a_target():
    """The operator asked for a target at all times. None is not an answer."""
    for age in (0.0, 5.0, 20.0, 100.0):
        for climbing in (True, False):
            t = dynamic_target(ENTRY, 100.0, COVERED, BASE, age, climbing=climbing,
                               decay_after_h=8, decay_per_h=0.004)
            assert isinstance(t, float) and t > 0


def test_a_decayed_target_can_never_ask_for_a_loss():
    """THE SAFETY PROPERTY. However long a trade stalls, the target stays above
    the price that returns the money paid -- so decay can only ever shrink the
    profit, never manufacture a loss. Booking losses is the stop's job."""
    for age in (9.0, 24.0, 100.0, 10_000.0):
        t = dynamic_target(ENTRY, 100.0, COVERED, BASE, age,
                           decay_after_h=8, decay_per_h=0.004)
        assert t >= COVERED, f"at {age}h the target {t} is below breakeven {COVERED}"


def test_the_target_only_falls_after_the_grace_period():
    base_px = ENTRY * (1 + BASE)
    assert dynamic_target(ENTRY, 100.0, COVERED, BASE, 7.9,
                          decay_after_h=8, decay_per_h=0.004) == pytest.approx(base_px)
    later = dynamic_target(ENTRY, 100.0, COVERED, BASE, 16.0,
                           decay_after_h=8, decay_per_h=0.004)
    assert later < base_px


def test_it_falls_monotonically_with_age():
    prev = None
    for age in (8.0, 12.0, 16.0, 20.0, 30.0):
        t = dynamic_target(ENTRY, 100.0, COVERED, BASE, age,
                           decay_after_h=8, decay_per_h=0.004)
        if prev is not None:
            assert t <= prev
        prev = t


def test_climbing_holds_the_target_above_the_price():
    """The lab half. Not live, but it must still be correct where it is used."""
    t = dynamic_target(ENTRY, 120.0, COVERED, BASE, 2.0, climbing=True, lead_frac=0.03)
    assert t > 120.0
    assert t == pytest.approx(120.0 * 1.03)


def test_the_engine_does_not_mutate_the_stored_target():
    """Decay is computed from the ORIGINAL each tick. If the engine wrote the
    decayed value back, it would compound every few seconds and a restart would
    leave the position with a permanently shrunken target."""
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index("eff_target = pos[\"target_px\"]")
    window = src[i:src.index("reason = None", i)]
    assert "UPDATE positions SET target_px" not in window
    assert "eff_target" in window


def test_the_engine_ships_only_the_half_that_was_measured():
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    # anchor on the CALL, not on a line 2k characters above it: a comment added
    # between the two silently slid the assertion off the end of its window.
    # anchored on the call's own closing paren rather than on a keyword that
    # may be replaced: `decay_per_h` left the call when the clock did.
    i = src.index("eff_target = dynamic_target(")
    call = src[i:src.index("\n", src.index("shrink=_shrink", i))]
    assert "climbing=False" in call, \
        "the raising half went live; it changed 0 of 85 replayed trades"
