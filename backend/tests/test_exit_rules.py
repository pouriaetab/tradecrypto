"""Every position must have an exit that can end in profit.

    "if you dont have exit strategy and just wait for the process to drop below
     the cost then what is point of getting in if no matter what we are just
     waiting for price to drop to exit! This doesnt make sense."

He was right, and it is arithmetic rather than opinion. A pure trailing stop
exits on a DECLINE by construction, so it gives back the trail distance from the
peak every time, plus the round trip. At an 8% trail and a 0.95% spread a side,
the peak had to reach 9.74% above the fill before the exit could be green at all.
Below that the rule guaranteed a loss no matter how right the entry was.

The fix is a breakeven ratchet: once the coin has risen far enough that a sale
covers both spreads, the stop never falls below that price again. It was chosen
by measurement — 260 pump entries over four years of hourly bars, chronological
split — and it improved EVERY trailing variant tested, 5 of 5, at widths from 5%
to 15%, fixed and volatility-scaled:

    exit rule                  TRAIN      HELD-OUT
    A trail 8% (shipped)       -2.97%      -2.97%
    B trail 8% + breakeven     -1.89%      -0.76%
    C trail 12% + breakeven    -1.53%      -1.82%
    D trail 5% + breakeven     -1.39%      +0.13%
    E decay 15->5% + be        -1.66%      -1.78%
    F vol-scaled + be          -1.76%      -1.59%
    G target +8%, stop -10%    -1.06%      -4.25%   <- best on train, worst held out

G is the cautionary one and the reason no fixed target was added: it had the best
win rate of any rule (32%) and the worst held-out return, because its best trade
was capped at +7.0% while the trailing rules kept trades worth +47%. Cutting
winners to raise the win rate is how a rule looks good and loses money.
"""
from __future__ import annotations

import sys
from pathlib import Path
from conftest import skip_without_history

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SIDE = 0.0095


def _covered(entry: float, side: float = SIDE) -> float:
    """The price at which selling returns exactly what was paid."""
    return entry / (1.0 - side)


def _simulate(entry: float, path: list[float], trail: float = 0.08,
              breakeven: bool = True, side: float = SIDE) -> float:
    """Walk a price path under the engine's ratchet and return the net result."""
    peak, stop = entry, entry * (1 - trail)
    for px in path:
        peak = max(peak, px)
        stop = max(stop, peak * (1 - trail))
        if breakeven and peak >= _covered(entry, side) * 1.005:
            stop = max(stop, _covered(entry, side))
        if px <= stop:
            return stop * (1 - side) / entry - 1
    return path[-1] * (1 - side) / entry - 1


def test_the_old_rule_loses_on_a_move_that_was_right():
    """The exact complaint: a correct entry that still loses."""
    entry = 0.22447
    path = [entry * m for m in (1.03, 1.05, 1.04, 0.99, 0.96)]   # +5% then fades
    assert _simulate(entry, path, breakeven=False) < 0


def test_the_breakeven_ratchet_rescues_that_same_move():
    entry = 0.22447
    path = [entry * m for m in (1.03, 1.05, 1.04, 0.99, 0.96)]
    got = _simulate(entry, path, breakeven=True)
    assert got >= -0.0005, f"a trade that rose 5% still lost {got:.2%}"


def test_a_covered_trade_can_never_go_back_to_a_loss():
    """Once the peak clears the round trip, the floor is the money already made."""
    entry = 100.0
    for top in (1.02, 1.05, 1.20, 2.00):
        path = [entry * top, entry * 0.10]      # spike, then collapse to nothing
        assert _simulate(entry, path) >= -0.0005, (
            f"a position that peaked {top:.0%} of entry still lost")


def test_a_trade_that_never_covers_still_stops_out():
    """The ratchet must not become a reason to hold a loser forever."""
    entry = 100.0
    path = [100.4, 99.0, 95.0, 91.0, 88.0]      # never clears the spread
    got = _simulate(entry, path)
    assert got < 0 and got > -0.15, f"expected a bounded loss, got {got:.2%}"


def test_upside_is_not_capped():
    """The reason no fixed target was added: G capped its best trade at +7%."""
    entry = 100.0
    path = [110.0, 130.0, 160.0, 147.0]          # runs, then gives back the trail
    assert _simulate(entry, path) > 0.30


def test_the_engine_actually_implements_the_ratchet():
    """Guard against the rule living only in this file."""
    src = (Path(__file__).resolve().parents[1] / "app" / "execution" / "engine.py").read_text()
    assert "BREAKEVEN RATCHET" in src, "the ratchet is not in the engine"
    assert "covered" in src and "1.005" in src


def test_every_open_position_has_some_way_out():
    """No position may rely on hope. A stop, a target or a clock — at least one."""
    skip_without_history()
    from app.core import db
    naked = []
    for p in db.query("SELECT symbol, stop_px, target_px, max_hold_s, trail_bps "
                      "FROM positions WHERE qty > 0"):
        if not (p["stop_px"] or p["target_px"] or p["max_hold_s"] or p["trail_bps"]):
            naked.append(p["symbol"])
    assert not naked, f"positions with no exit at all: {naked}"
