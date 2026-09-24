"""Position size must never shrink because the desk got chattier.

2026-09-22. `base_share` was `1 / desk_signals_per_day()`. The universe grew
from 33 coins to 75, the same unchanged rule (day_climb v0.1, params
c3ba79714a) found setups in 29 coins instead of 6, the denominator tripled, and
every strategy's position halved -- from $172 to $64 -- with nobody deciding it.
By Sep 21 the standard deviation of position size within a day was $0.34: the
book had become one bet repeated, which is a machine for averaging the day's
result to zero.

Every test here fails if that coupling comes back.
"""
import pytest

from app.core import db
from app.feedback import sizing


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def _signals(strategy, n, symbols=1, day=0):
    """n signal hours for a strategy, spread over `symbols` coins."""
    import time as _t
    base = _t.time() - day * 86400.0
    for i in range(n):
        db.execute(
            """INSERT INTO signals(ts, strategy, strategy_version, symbol, side, raw_score,
                                   expected_edge_bps, cost_hurdle_bps, decision)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (base - i * 3600.0, strategy, "0.1", f"C{i % symbols}", "buy", 1.0, 0.0, 190.0,
             "rejected"))


def test_share_does_not_move_when_the_desk_gets_chattier():
    """THE REGRESSION. This is the whole bug in one assertion.

    A QUIET desk first -- a handful of signal hours spread over several days, so
    the old `1 / signals per day` rule would have returned a LARGE share. Then a
    loud one. If anything reintroduces the coupling, these two differ.
    """
    _signals("day_climb", 3, symbols=1, day=3)
    _signals("day_climb", 2, symbols=1, day=2)
    quiet = sizing.base_share("day_climb")
    assert quiet > sizing.SHARE_FLOOR, (
        "a quiet desk is already pinned at the floor, so this test cannot tell "
        "coupling from clipping -- fix the fixture, not the assertion")
    _signals("day_climb", 200, symbols=40)
    _signals("morning_dip", 200, symbols=40)
    _signals("volume_build", 200, symbols=40)
    loud = sizing.base_share("day_climb")
    assert loud == quiet, (
        f"position share fell from {quiet:.3f} to {loud:.3f} because more signals "
        f"fired -- size is coupled to activity again")


def test_share_is_the_concentration_cap():
    assert sizing.base_share("anything") == sizing.SHARE_CAP
    assert 0.0 < sizing.SHARE_CAP <= 0.5, "a single position taking over half the free cash is not a cap"


def test_share_is_the_same_for_every_strategy():
    """It is a property of the BOOK, not of a strategy's talkativeness."""
    _signals("day_climb", 100, symbols=20)
    _signals("pump_ride", 2, symbols=1)
    assert sizing.base_share("day_climb") == sizing.base_share("pump_ride")


def _fills(day_counts):
    import time as _t
    now = _t.time()
    k = 0
    for d, n in enumerate(day_counts):
        for i in range(n):
            k += 1
            db.execute(
                """INSERT INTO orders(client_id, strategy, symbol, side, intent, qty,
                                      notional_usd, mode, status, ts_decided, ts_filled)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (f"c{k}", "day_climb", "C", "buy", "open", 1.0, 50.0, "paper",
                 "filled", now - d * 86400.0, now - d * 86400.0 - i * 60.0))


def test_one_busy_day_does_not_halve_every_later_trade():
    """The mean over a trailing window stays dragged by a single burst. On
    2026-09-20 this desk filled 26 times in a day; the mean went to 12 and cut
    every later trade's risk budget in half. The median does not move."""
    _fills([3, 3, 3, 3, 26])
    assert sizing.fills_per_day() == 3.0, "fills_per_day is following the outlier again"


def test_fills_per_day_is_never_below_one():
    assert sizing.fills_per_day() >= 1.0


def test_risk_budget_is_floored_however_busy_the_desk_gets():
    """The second runaway: more fills -> smaller budget -> smaller positions ->
    cash left over -> room for more fills. That loop has no bottom."""
    _fills([200, 200, 200])
    budget, why = sizing.risk_budget_per_trade("paper")
    from app.risk import guards
    cap = guards.daily_loss_limit_usd()
    assert budget == pytest.approx(cap / sizing.MAX_FILLS_FOR_BUDGET)
    assert "floored" in why


def test_a_quiet_desk_still_gets_the_plain_division():
    _fills([4, 4, 4])
    budget, why = sizing.risk_budget_per_trade("paper")
    from app.risk import guards
    assert budget == pytest.approx(guards.daily_loss_limit_usd() / 4.0)
    assert "floored" not in why


def test_equal_risk_is_what_actually_decides_the_size():
    """The cash share is a CEILING. If it binds on an ordinary trade, the book
    is being sized by a cap instead of by risk, which is how this went wrong."""
    _fills([6, 6, 6])
    budget, _ = sizing.risk_budget_per_trade("paper")
    by_risk, _ = sizing.equal_risk_notional(0.08, "paper")
    # $1,000 of free cash was chosen when the risk budget was a quarter of a $60
    # cap. Raising the cap raised the budget, so the fixed cash figure stopped
    # being the larger of the two and the test began asserting the opposite of
    # its own point. Size the cash so the comparison is the one described.
    cash = max(1000.0, by_risk / max(sizing.base_share("day_climb"), 1e-9) * 2.0)
    by_cash = cash * sizing.base_share("day_climb")
    assert by_risk < by_cash, (
        f"on $1,000 free cash an ordinary 8% stop sizes to ${by_risk:.2f} by risk but "
        f"the cap allows only ${by_cash:.2f} -- the cap is binding, not bounding")


def test_the_book_tapers_without_any_slot_count():
    """Operator removed slot caps deliberately. Concentration on FREE cash gives
    a taper for free: each position is smaller than the last, and the day ends
    when a share drops under the venue minimum."""
    cash, sizes = 1000.0, []
    for _ in range(12):
        s = cash * sizing.SHARE_CAP
        if s < 10.0:
            break
        sizes.append(s)
        cash -= s
    assert len(sizes) >= 6, "the book cannot get deep enough to diversify"
    assert len(sizes) <= 20, "the book never stops opening positions"
    assert all(sizes[i] > sizes[i + 1] for i in range(len(sizes) - 1)), "sizes must taper"
