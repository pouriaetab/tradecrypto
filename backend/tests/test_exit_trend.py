"""Riding a trend instead of trading round it — the operator's idea, measured.

    "keep changing and increasing stop loss for these and at time to go above
     the target rate to squeeze more when it has the potential instead of ...
     keep doing multiple buy and sell"

These tests pin the MECHANISM. Whether it makes money is not a question a test
can answer — that is what the replay is for, and at 32 closed trades the lab's
own bar (MIN_FOR_A_VERDICT = 40) has not been met.
"""
from __future__ import annotations

import pytest

from app.research import exit_lab as E


ARGS = dict(entry=100.0, peak=112.0, covered=101.92, age_h=6.0,
            atr_frac=0.01, bars_since_peak_h=0.5)


class TestTheTrendChangesTheExit:

    def test_a_climbing_coin_has_its_target_released(self):
        fn = E.RULES["ride_nofloor_n6"]["fn"]
        _, tgt_flat = fn(**ARGS, trending=False, state={})
        _, tgt_climb = fn(**ARGS, trending=True, state={})
        assert tgt_flat is not None, "the control rule has no target to release"
        assert tgt_climb is None, "the target was not released while climbing"

    def test_a_climbing_coin_does_not_have_its_trail_tightened_by_age(self):
        """Age is not evidence against a trade that is still going up."""
        fn = E.RULES["ride_nofloor_n6"]["fn"]
        old = dict(ARGS); old["age_h"] = 12.0
        stop_flat, _ = fn(**old, trending=False, state={})
        stop_climb, _ = fn(**old, trending=True, state={})
        assert stop_climb < stop_flat, \
            "the ageing trail still tightened while the coin was climbing"

    def test_the_stop_never_moves_down(self):
        """The trade gives up a known profit, not its protection."""
        fn = E.RULES["ride_nofloor_n6"]["fn"]
        stop, _ = fn(**ARGS, trending=True, state={})
        assert stop >= ARGS["entry"] * (1 - 0.10), "the stop fell through the hard floor"
        rising = dict(ARGS); rising["peak"] = 130.0
        stop2, _ = fn(**rising, trending=True, state={})
        assert stop2 > stop, "a higher peak did not raise the stop"


class TestReleasingTheTargetIsOneWay:
    """The first version released the target while climbing and restored it the
    moment the climb paused. On UNI the target came back three bars later, still
    below the price, and the trade exited at the same price for the same money:
    every trend variant scored EXACTLY its own control, to four decimals. A
    target that returns is not a target you released."""

    def test_once_released_it_stays_released(self):
        fn = E.RULES["ride_nofloor_n6"]["fn"]
        st = {}
        _, tgt_climb = fn(**ARGS, trending=True, state=st)
        assert tgt_climb is None
        _, tgt_after = fn(**ARGS, trending=False, state=st)
        assert tgt_after is None, "the target came back when the climb paused"

    def test_a_trade_that_never_trended_keeps_its_target(self):
        fn = E.RULES["ride_nofloor_n6"]["fn"]
        st = {}
        for _ in range(5):
            _, tgt = fn(**ARGS, trending=False, state=st)
        assert tgt is not None, "the target vanished without any trend at all"

    def test_a_fresh_state_object_is_used_for_each_rule_and_trade(self, monkeypatch):
        """One trade's released target must not leak into the next.

        This goes through replay() ON PURPOSE. The first version of this test
        built its own two dicts and passed them in by hand, so it proved that
        two different dicts are different -- it stayed GREEN when replay() was
        re-broken to share one state object across every trade, which is the
        exact bug it was written to catch.
        """
        from app.core import db
        seen = []

        # **kw, so this probe does not break every time the replay learns to
        # pass the rules one more thing about the bar.
        def probe(entry, peak, covered, age_h, atr_frac, bars_since_peak_h,
                  trending=False, state=None, **kw):
            seen.append(id(state))
            return entry * 0.5, None          # never exits; walks every bar

        monkeypatch.setattr(E, "RULES", {"probe": {"fn": probe, "what": "probe"}})
        from conftest import skip_without
        skip_without("bars")
        trades = db.query("SELECT * FROM trades WHERE ts_close IS NOT NULL "
                          "ORDER BY id LIMIT 2")
        if len(trades) < 2:
            pytest.skip("needs two closed trades with bars")
        for t in trades:
            E.replay(dict(t))
        assert len(set(seen)) >= 2, \
            "every trade was handed the same state object — a release would leak"


class TestTheControlsExist:
    """A variant with nothing to compare against measures nothing."""

    def test_every_trend_rule_has_a_matching_control(self):
        for name in ("ride_trend_n6", "ride_nofloor_n6"):
            assert name in E.RULES
        for ctrl in ("ride_trend_off", "ride_nofloor_off"):
            assert ctrl in E.RULES, f"{ctrl} is missing — the comparison is meaningless"
            assert not E.RULES[ctrl].get("trend_n"), "the control is not a control"

    def test_the_trend_test_cannot_see_the_future(self):
        """A trend test with hindsight makes any rule look brilliant."""
        import inspect
        src = inspect.getsource(E.replay)
        assert "closes[-n:]" in src and "lows[-2 * n:-n]" in src, \
            "the trend windows are no longer taken from bars up to NOW only"
        assert "bars[i + 1:]" not in src and "future" not in src.lower().split("#")[0]

    def test_nothing_here_can_reach_live_trading_on_its_own(self):
        """These are lab rules. Promotion is a separate, gated decision."""
        assert E.MIN_FOR_A_VERDICT >= 40


# ── the timeout family ───────────────────────────────────────────────────────
# day_climb's 59 closed trades, 2026-09-22: 57% hit target for +$124.14, and 27%
# ran the clock out for -$57.97. The operator called it "getting stuck". These
# rules ask whether cutting a trade that is too slow recovers any of that.

def test_a_slow_trade_is_cut_at_the_chosen_hour():
    from app.research.exit_lab import _mk
    rule = _mk(trail=0.08, target=0.08, progress_h=8.0, progress_frac=0.50)
    # 1% of the way up against an 8% target: nowhere near half.
    stop, _tgt = rule(entry=100.0, peak=101.0, covered=0.0, age_h=8.5, atr_frac=0.02,
                bars_since_peak_h=0.0, state={})
    assert stop > 1e17, "a trade far behind its target was not cut at the hour"


def test_a_trade_making_progress_is_left_alone():
    from app.research.exit_lab import _mk
    rule = _mk(trail=0.08, target=0.08, progress_h=8.0, progress_frac=0.50)
    stop, _tgt = rule(entry=100.0, peak=106.0, covered=0.0, age_h=8.5, atr_frac=0.02,
                bars_since_peak_h=0.0, state={})
    assert stop < 1e17, "a trade 75% of the way to target was cut anyway"


def test_the_rule_does_nothing_before_its_hour():
    from app.research.exit_lab import _mk
    rule = _mk(trail=0.08, target=0.08, progress_h=8.0, progress_frac=0.50)
    stop, _tgt = rule(entry=100.0, peak=100.2, covered=0.0, age_h=2.0, atr_frac=0.02,
                bars_since_peak_h=0.0, state={})
    assert stop < 1e17, "the cut fired before the trade had been given its time"


def test_the_control_never_cuts():
    """`cut_off` is the same shape with the rule switched off. If it ever cuts,
    the family is comparing two different things and every number is wrong."""
    from app.research.exit_lab import _mk
    rule = _mk(trail=0.08, target=0.08)
    for age in (1.0, 8.5, 23.0):
        stop, _tgt = rule(entry=100.0, peak=100.1, covered=0.0, age_h=age, atr_frac=0.02,
                    bars_since_peak_h=0.0, state={})
        assert stop < 1e17, f"the control cut a trade at {age}h"


def test_the_family_is_registered_with_its_control():
    from app.research.exit_lab import RULES
    for k in ("cut_off", "cut_4h_under_25", "cut_8h_under_25",
              "cut_8h_under_50", "cut_12h_under_50"):
        assert k in RULES, f"{k} is not in the lab's roster"
        assert RULES[k].get("what"), f"{k} does not say what it is"


# ── the ceiling and the floor ────────────────────────────────────────────────
# "is there any way we can aim for the best it has been column? or is it not
# possible because these are the numbers after the events?" -- the operator,
# 2026-09-22. He is right that no rule can exit at a peak. These two bounds say
# how much is in the gap, so the question of whether exit tuning is worth more
# effort is answered with a number instead of an opinion.

def _seed_a_shape(entry=1.0, peak=1.20, trough=0.80, last=1.00):
    """A trade whose coin climbs to a clear peak, falls to a clear trough, and
    ends between them. Every number below is checked against THIS shape, so a
    bound that stops tracking the bars fails instead of quietly agreeing."""
    import time
    from app.core import db
    t0 = time.time() - 30 * 3600
    path = ([entry + (peak - entry) * i / 10 for i in range(11)]          # up
            + [peak - (peak - trough) * i / 15 for i in range(1, 16)]     # down
            + [trough + (last - trough) * i / 14 for i in range(1, 15)])  # back
    for i, px in enumerate(path):
        db.execute("INSERT INTO bars(symbol, granularity, ts, open, high, low, close, volume, source) "
                   "VALUES ('BND', 60, ?, ?, ?, ?, ?, 1000, 'test')",
                   (t0 + i * 60, px, px, px, px))
    return {"id": 1, "symbol": "BND", "ts_open": t0, "entry_px": entry}, t0, path


def test_the_ceiling_is_the_best_price_the_trade_ever_saw(writable_db):
    """Not "a high price", not "the close" -- the maximum, at the bar it happened."""
    from app.research import exit_lab
    trade, t0, path = _seed_a_shape()
    rows = {r["rule"]: r for r in exit_lab.replay(trade)}
    side = exit_lab._side_pct("BND")

    ceiling, floor = rows[exit_lab.BOUNDS[0]], rows[exit_lab.BOUNDS[1]]
    assert ceiling["net_pct"] == pytest.approx((1.20 * (1 - side) / 1.0 - 1) * 100.0)
    assert floor["net_pct"] == pytest.approx((0.80 * (1 - side) / 1.0 - 1) * 100.0)
    # and at the right MOMENT: the peak is bar 10, the trough bar 25
    assert ceiling["hours_held"] == pytest.approx(10 * 60 / 3600.0)
    assert floor["hours_held"] == pytest.approx(25 * 60 / 3600.0)


def test_no_rule_can_beat_the_ceiling_or_undercut_the_floor(writable_db):
    """The point of the bounds. A rule that scores above the ceiling is reading
    a price that never traded, and every gap measured against it is fiction."""
    from app.research import exit_lab
    trade, _t0, _path = _seed_a_shape()
    rows = exit_lab.replay(trade)
    by = {r["rule"]: r["net_pct"] for r in rows}
    hi, lo = by[exit_lab.BOUNDS[0]], by[exit_lab.BOUNDS[1]]
    assert hi > lo
    graded = [(k, v) for k, v in by.items() if k not in exit_lab.BOUNDS]
    assert len(graded) >= 20, "the rules did not run -- this proves nothing"
    for name, pct in graded:
        assert pct <= hi + 1e-9, f"{name} scored {pct:.4f}%, above perfect hindsight {hi:.4f}%"
        assert pct >= lo - 1e-9, f"{name} scored {pct:.4f}%, below the worst price {lo:.4f}%"


def test_the_bounds_are_not_tradeable_rules(writable_db):
    """They must never appear on the standings as something the desk could run.
    Out of the rule tables, and marked hindsight in their own reason string."""
    from app.research import exit_lab
    trade, _t0, _path = _seed_a_shape()
    rows = {r["rule"]: r for r in exit_lab.replay(trade)}
    for b in exit_lab.BOUNDS:
        assert b.startswith("_"), "the bound lost its underscore and now sorts among the rules"
        assert b not in exit_lab.RULES, f"{b} is in RULES -- it would be reported as tradeable"
        assert b not in exit_lab.SCALE_RULES
        assert "hindsight" in rows[b]["exit_reason"]


def test_the_bounds_charge_the_same_round_trip_as_every_rule(writable_db):
    """A ceiling that does not pay costs is not on the same scoreboard, and the
    gap it reports would be flattering nonsense."""
    from app.research import exit_lab
    trade, _t0, _path = _seed_a_shape()
    rows = {r["rule"]: r for r in exit_lab.replay(trade)}
    side = exit_lab._side_pct("BND")
    assert side > 0, "no spread to charge -- the rest of this test is vacuous"
    raw_gain = (1.20 / 1.0 - 1) * 100.0                 # mid-to-mid, costs ignored
    booked = rows[exit_lab.BOUNDS[0]]["net_pct"]
    assert booked < raw_gain, "the ceiling is not charging the sell spread"
    assert raw_gain - booked == pytest.approx(1.20 * side * 100.0)
