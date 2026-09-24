"""day_climb keeps a profit target (2026-09-23, reverted same day).

The operator: "No have target for exit on all strategies. I dont like the no
target." Reverted. `use_target` stays as the switch so the lab can still run
the no-target arm, and the dynamic target is built on top of it.

Measured on its own 84 replayed trades: hold-with-no-target +2.797%/trade vs
+0.411% for the exits actually taken -- +2.39 points at 2.4 sigma. The stop and
the 24h clock are unchanged, so exactly one thing flipped.
"""
from app.strategy.day_climb import DayClimb


def test_the_target_is_on_by_default():
    assert DayClimb.defaults()["use_target"] is True


def test_the_stop_still_scales_with_the_target_width():
    """target_vol_mult / floor / ceiling are NOT dead parameters -- they still
    set the stop through conviction_stop_bps. Deleting them would silently
    change the stop on every signal."""
    d = DayClimb.defaults()
    for k in ("target_vol_mult", "target_floor_pct", "target_ceiling_pct"):
        assert k in d, f"{k} was removed; the stop width depends on it"


def test_turning_it_back_on_is_one_parameter():
    """The operator must be able to revert without a code change."""
    import inspect
    from app.strategy import day_climb
    src = inspect.getsource(day_climb)
    assert 'p.get("use_target")' in src
    assert "target_bps=(float(target_pct) * 100.0" in src


def test_the_catastrophe_stop_and_clock_are_untouched():
    d = DayClimb.defaults()
    assert d["catastrophe_stop_pct"] == 8.0
    assert d["max_hold_hours"] == 24
