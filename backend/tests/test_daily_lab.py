"""The lab scores every strategy daily and acts on the score (2026-09-23)."""
import pytest

from app.research import daily_lab


def _seed(sym, strat, pnls, basis=100.0):
    from app.core import db
    import time
    t = time.time() - 3600
    for i, n in enumerate(pnls):
        db.execute("INSERT INTO trades(symbol,strategy,mode,qty,entry_px,exit_px,ts_open,"
                   "ts_close,holding_s,gross_pnl_usd,cost_usd,net_pnl_usd) "
                   "VALUES (?,?,'paper',1,?,?,?,?,3600,?,0,?)",
                   (sym, strat, basis, basis + n, t - 3600 + i, t + i, n, n))


def test_a_clear_loser_is_switched_off(writable_db):
    _seed("AAA", "loser", [-3.0, -2.5, -3.5, -2.0, -4.0] * 4)
    out = daily_lab.run("paper", act=True)
    assert "loser" in out["demoted"]
    from app.risk import control
    assert control.state("loser")["enabled"] is False


def test_a_few_bad_trades_are_not_enough(writable_db):
    """Three losses is not evidence. MIN_TRADES is what stops the lab acting on
    noise, and it is the difference between a scorecard and a panic."""
    _seed("BBB", "unlucky", [-3.0] * 3)
    out = daily_lab.run("paper", act=True)
    assert "unlucky" not in out["demoted"]
    s = next(x for x in out["scored"] if x["strategy"] == "unlucky")
    assert "not enough trades" in s["verdict"]


def test_a_winner_is_left_alone(writable_db):
    _seed("CCC", "winner", [2.0] * 20)
    out = daily_lab.run("paper", act=True)
    assert "winner" not in out["demoted"]


def test_it_judges_on_percent_not_dollars(writable_db):
    """A strategy given bigger positions must not score better for that alone."""
    _seed("DDD", "small", [-1.0] * 20, basis=50.0)
    _seed("EEE", "big", [-2.0] * 20, basis=100.0)
    a = daily_lab.score("small")
    b = daily_lab.score("big")
    assert a["mean_pct"] == pytest.approx(b["mean_pct"], abs=0.01), \
        "the same percentage loss scored differently because of position size"


def test_the_operator_outranks_the_lab(writable_db):
    """THE SAFETY PROPERTY. If he switched something ON, the lab may not switch
    it off behind his back."""
    from app.risk import control
    _seed("FFF", "his_pick", [-3.0] * 20)
    control.set_state("his_pick", enabled=True, note="operator wants this on")
    daily_lab.run("paper", act=True)
    assert control.state("his_pick")["enabled"] is True


def test_the_worst_hour_is_recorded_not_just_the_day(writable_db):
    """2026-09-23: five stops fired in 98 minutes for -$49.12 -- the whole day's
    loss. A daily total hides that; the worst hour names it."""
    _seed("GGG", "s", [-10.0] * 5)
    b = daily_lab.book_day()
    assert b["trades"] == 5
    assert b["stops_in_worst_hour"] >= 1
    assert b["worst_hour_usd"] < 0


def test_the_scorecard_is_written_down(writable_db):
    from app.core import db
    _seed("HHH", "kept", [1.0] * 20)
    daily_lab.run("paper", act=True)
    row = db.query_one("SELECT * FROM lab_scores WHERE strategy='kept'")
    assert row and row["n"] == 20 and row["verdict"]


def test_zero_variance_is_maximum_certainty_not_zero(writable_db):
    """A strategy that loses the same amount every single time is the surest
    loser there is. `mean / (0/sqrt(n))` is a divide-by-zero, and the obvious
    guard turns it into t = 0 -- the score of a coin flip. It would never be
    switched off."""
    _seed("III", "always_loses", [-3.0] * 20)
    s = daily_lab.score("always_loses")
    assert s["sd_pct"] == 0.0
    assert s["t_stat"] < daily_lab.DEMOTE_T, \
        "a perfectly consistent loser scored as undecided"
    out = daily_lab.run("paper", act=True)
    assert "always_loses" in out["demoted"]


def test_zero_variance_winner_is_not_switched_off(writable_db):
    _seed("JJJ", "always_wins", [3.0] * 20)
    assert daily_lab.score("always_wins")["t_stat"] > 0
