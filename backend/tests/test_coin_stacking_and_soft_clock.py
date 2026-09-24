"""Two strategies in one coin, and the 24-hour wall.

2026-09-22. The operator, looking at the open book: CHIP held by volume_build
and pump_catch entered in the SAME MINUTE, SHIB five minutes apart, NEAR and UNI
doubled too -- $476 of a ~$2,000 book in four coins. "Not at the same time,
entering the same stock with two different strategies. That doesn't make sense."

And: "the 24 hours ... should not be a hard limit ... if we are very close to
getting break even, waiting four or five hours after is okay. But if we are
clearly seeing the new day cycle going down, then just get out."
"""
import inspect

import pytest

from app.core import db


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


# ── the block ────────────────────────────────────────────────────────────────

def _pos(symbol, strategy):
    db.execute("""INSERT INTO positions(symbol, mode, strategy, qty, avg_px, opened_ts)
                  VALUES (?,?,?,?,?,?)""", (symbol, "paper", strategy, 1.0, 10.0, 1.0))


def test_a_second_strategy_may_not_enter_a_coin_already_held():
    """THE ASK."""
    from app.risk import guards
    _pos("CHIP", "volume_build")
    d = guards.pre_trade_check(symbol="CHIP", side="buy", notional_usd=40.0,
                               mode="paper", intent="open", strategy="pump_catch")
    failed = [c for c in d.checks if not c["passed"]]
    assert any(c["check"] == "coin_stacking" for c in failed), \
        "a second strategy walked into a coin another was already holding"


def test_the_same_coin_is_fine_once_the_other_position_is_gone():
    from app.risk import guards
    d = guards.pre_trade_check(symbol="CHIP", side="buy", notional_usd=40.0,
                               mode="paper", intent="open", strategy="pump_catch")
    assert not any(c["check"] == "coin_stacking" and not c["passed"] for c in d.checks)


def test_the_refusal_names_the_lab_so_it_can_be_found_later():
    """The refusal IS the data collection — the reason text is what the study
    searches for. If the wording drifts, the lab goes quietly empty."""
    from app.risk import guards
    from app.research import coin_stacking
    _pos("SHIB", "pump_catch")
    d = guards.pre_trade_check(symbol="SHIB", side="buy", notional_usd=40.0,
                               mode="paper", intent="open", strategy="volume_build")
    why = " ".join(c["detail"] for c in d.checks if not c["passed"])
    assert coin_stacking.REASON_MARK in why, \
        "the refusal no longer carries the marker the study looks for"


def test_an_exit_is_never_blocked_by_the_stacking_pause():
    """Blocking a SALE because two strategies hold the coin would strand money."""
    from app.risk import guards
    _pos("CHIP", "volume_build")
    d = guards.pre_trade_check(symbol="CHIP", side="sell", notional_usd=40.0,
                               mode="paper", intent="close", strategy="pump_catch")
    assert not any(c["check"] == "coin_stacking" and not c["passed"] for c in d.checks)


def test_the_study_says_nothing_rather_than_guessing_on_no_data():
    from app.research import coin_stacking
    out = coin_stacking.study()
    assert out["available"] is False
    assert out["why"]


# ── the soft clock and the stuck-position release ────────────────────────────

def _rule(**kw):
    from app.research.exit_lab import _mk
    return _mk(trail=0.08, target=0.08, **kw)


def _fires(rule, **kw):
    base = dict(entry=100.0, peak=100.0, covered=0.0, age_h=1.0, atr_frac=0.02,
                bars_since_peak_h=0.0, state={})
    base.update(kw)
    stop, _ = rule(**base)
    return stop > 1e17


def test_an_old_position_back_near_entry_is_released():
    r = _rule(release_h=12.0, release_band=0.01)
    assert _fires(r, age_h=13.0, peak=100.5), "a 13h trade back at breakeven was held anyway"


def test_it_leaves_a_young_position_alone():
    r = _rule(release_h=12.0, release_band=0.01)
    assert not _fires(r, age_h=6.0, peak=100.5)


def test_it_leaves_a_position_that_is_still_trending():
    """The operator's own exception: 'except the ones that have momentum'."""
    r = _rule(release_h=12.0, release_band=0.01, trend_n=3)
    assert not _fires(r, age_h=13.0, peak=100.5, trending=True)


def test_the_soft_clock_extends_a_near_breakeven_trade_past_24h():
    r = _rule(hard_h=24.0, release_band=0.01, grace_h=4.0)
    assert not _fires(r, age_h=24.5, peak=100.5), "sold at the clock despite being at breakeven"


def test_the_soft_clock_still_sells_a_trade_that_is_nowhere_near():
    r = _rule(hard_h=24.0, release_band=0.01, grace_h=4.0)
    assert _fires(r, age_h=24.5, peak=94.0), "a trade 6% down was given grace"


def test_the_soft_clock_cuts_anyway_when_the_day_has_turned_bear():
    """'if we are clearly seeing the new day cycle going down, then just get out'."""
    r = _rule(hard_h=24.0, release_band=0.01, grace_h=4.0)
    assert _fires(r, age_h=24.5, peak=100.5, bear=True)


def test_the_grace_itself_runs_out():
    r = _rule(hard_h=24.0, release_band=0.01, grace_h=4.0)
    assert _fires(r, age_h=28.5, peak=100.5), "grace never expired"


def test_the_hard_control_sells_at_24h_whatever_happens():
    r = _rule(hard_h=24.0, release_band=0.0, grace_h=0.0)
    assert _fires(r, age_h=24.1, peak=100.0)


def test_the_lab_can_see_past_the_hard_hold():
    """A rule that can never be shown hour 25 cannot be measured on hour 25."""
    from app.research import exit_lab
    assert exit_lab.GRACE_H > 0
    src = inspect.getsource(exit_lab.replay)
    assert "GRACE_H" in src, "the replay window still stops dead at the hard hold"


def test_every_new_family_has_its_control():
    from app.research.exit_lab import RULES
    for k in ("cut_off", "release_12h_be", "release_15h_be", "release_12h_wide",
              "clock_hard_24h", "clock_soft_24h_4", "clock_soft_24h_8"):
        assert k in RULES and RULES[k].get("what")
