"""The liquidity gate is derived from the order we place, not a typed dollar floor.

Incident, 2026-09-19: the gate said "our $100 order must be a rounding error in
$2M/day". Both figures were hand-written; the book was $2,000 and the largest
open $252, and neither number reached the gate. On the live universe the fixed
floor was refusing 41 coins whose actual impact from a $225 order was under
20 bps against a ~95 bps spread. The gate now reads the order size from the
orders table and passes when sqrt-law impact fits inside the coin's own cost
uncertainty, capped at its one-side spread.
"""
from types import SimpleNamespace

from app.data import selection


def _cost(markup=95.0, ci_high=190.0):
    return SimpleNamespace(markup_bps=markup, ci_high_bps=ci_high,
                           round_trip_bps=2 * markup, status="measured")


def test_small_order_in_deep_coin_passes():
    g = selection._impact_gate(dv_day=5_000_000, vol_bps_hr=60.0, order_usd=225.0, cost=_cost())
    assert g["passed"]
    assert g["impact_bps"] < g["tolerance_bps"]


def test_order_that_would_move_the_coin_more_than_its_spread_fails():
    # $225 into a coin doing $2,400 a DAY ($100/hour): participation > 2x.
    g = selection._impact_gate(dv_day=2_400, vol_bps_hr=120.0, order_usd=225.0, cost=_cost())
    assert not g["passed"]
    assert g["impact_bps"] > g["tolerance_bps"]


def test_tolerance_never_exceeds_the_spread_however_uncertain_the_cost_is():
    # An "assumed" coin carries an error bar of ~1,000 bps. That must not
    # license 1,000 bps of impact.
    g = selection._impact_gate(dv_day=5_000_000, vol_bps_hr=60.0, order_usd=225.0,
                               cost=_cost(markup=95.0, ci_high=1200.0))
    assert g["tolerance_bps"] == 95.0


def test_no_volume_or_volatility_reading_is_a_fail_not_a_pass():
    assert not selection._impact_gate(0.0, 60.0, 225.0, _cost())["passed"]
    assert not selection._impact_gate(5e6, None, 225.0, _cost())["passed"]


def test_impact_grows_with_order_size_and_shrinks_with_depth():
    a = selection._impact_gate(5e6, 60.0, 100.0, _cost())["impact_bps"]
    b = selection._impact_gate(5e6, 60.0, 400.0, _cost())["impact_bps"]
    c = selection._impact_gate(20e6, 60.0, 400.0, _cost())["impact_bps"]
    assert abs(b / a - 2.0) < 1e-9          # sqrt law: 4x size -> 2x impact
    assert abs(b / c - 2.0) < 1e-9          # 4x depth -> half the impact


def test_gate_row_reports_the_old_floor_for_comparison():
    m = {"dollar_volume_day": 500_000.0, "vol_bps": 60.0, "completeness_pct": 100.0, "bars": 5000}
    score, gates = selection._score("X", m, _cost(), dict(selection.DEFAULTS), 225.0, "test")
    liq = next(g for g in gates if g["gate"] == "liquidity")
    assert liq["passed"] and liq["old_fixed_floor_would_pass"] is False
    assert liq["order_usd"] == 225.0


def test_cost_gate_is_the_coins_own_day_not_a_fixed_ceiling():
    """XTZ at a 2.12% round trip moving 5% a day passes; a 1.90% coin that
    moves 1.5% a day does not. 2026-09-18..21: XTZ +32%, +21%, BONK +10%,
    XPL +9% all sat in 'no strategy watching' under the fixed 2.00% ceiling."""
    from app.data import selection
    cfg = dict(selection.DEFAULTS)
    lively = {"dollar_volume_day": 5e6, "vol_bps": 60.0, "completeness_pct": 100.0, "bars": 5000,
              "daily_range_pct": 5.0}
    quiet = {**lively, "daily_range_pct": 1.5}
    _, g1 = selection._score("XTZ", lively, _cost(markup=106.0, ci_high=212.0), cfg, 225.0, "t")
    _, g2 = selection._score("DEAD", quiet, _cost(markup=95.0, ci_high=190.0), cfg, 225.0, "t")
    c1 = next(g for g in g1 if g["gate"] == "cost")
    c2 = next(g for g in g2 if g["gate"] == "cost")
    assert c1["passed"] and c1["old_fixed_ceiling_would_pass"] is False
    assert not c2["passed"] and c2["old_fixed_ceiling_would_pass"] is True
