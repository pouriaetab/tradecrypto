"""The day's P&L is the BOOK's P&L, not every row in the trades table.

2026-09-22. burst_catch was retired to mode='lab' -- out of the book, kept for
the lab -- and `daily_report` had no mode filter. Its 21 trades were still
counted into the paper day, so the Daily tab read **-$42.65** when the book had
actually lost **$18.41**: 40 trades instead of 19, $44.11 of spread instead of
$22.54, a 27.5% win rate instead of 42%.

Introducing a new value into a partitioning column means auditing every reader.
These tests are that audit, kept.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.core import db

TZ = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def _trade(mode, net, cost=1.0, when=None):
    ts = when if when is not None else dt.datetime.now(TZ).replace(hour=12).timestamp()
    db.execute(
        """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px,
                              ts_open, ts_close, holding_s, gross_pnl_usd,
                              cost_usd, net_pnl_usd)
           VALUES ('C','s',?,1.0,10.0,10.0,?,?,60.0,?,?,?)""",
        (mode, ts - 3600, ts, net + cost, cost, net))


def _pnl_today():
    """Exactly the query daily_report uses for the day's money."""
    from app.core import mode as mode_mod
    now = dt.datetime.now(TZ)
    t0 = dt.datetime(now.year, now.month, now.day, tzinfo=TZ).timestamp()
    rows = db.query(
        """SELECT gross_pnl_usd, cost_usd, net_pnl_usd FROM trades
           WHERE ts_close >= ? AND ts_close < ? AND mode = ?""",
        (t0, t0 + 86400, mode_mod.get_mode()))
    return (len(rows),
            sum(float(r["net_pnl_usd"] or 0) for r in rows),
            sum(float(r["cost_usd"] or 0) for r in rows))


def test_lab_trades_are_not_in_the_days_pnl():
    """THE REGRESSION."""
    _trade("paper", -2.0, cost=1.0)
    _trade("paper", +3.0, cost=1.0)
    for _ in range(5):
        _trade("lab", -4.0, cost=2.0)
    n, net, cost = _pnl_today()
    assert n == 2, f"the day counted {n} trades; the lab ones are back in the book"
    assert net == pytest.approx(1.0)
    assert cost == pytest.approx(2.0)


def test_a_book_with_only_lab_trades_reads_as_a_flat_day():
    for _ in range(3):
        _trade("lab", -9.0, cost=3.0)
    n, net, _ = _pnl_today()
    assert (n, net) == (0, 0.0)


def test_the_report_queries_actually_carry_the_filter():
    """The unit above proves the arithmetic. This proves the SHIPPED query has
    it -- the failure that happened was a correct formula on an unfiltered set."""
    import inspect
    from app.research import daily_report
    for fn in (daily_report.build, daily_report._last_activity):
        src = inspect.getsource(fn)
        for stmt in src.split("FROM trades")[1:]:
            head = stmt[:220]
            assert "mode" in head, (
                f"{fn.__name__} reads trades without scoping to a mode:\n{head}")


def test_research_paths_deliberately_still_see_every_mode():
    """The other half of the rule. exit_lab must keep reading retired trades --
    that is the whole reason retiring moves them instead of deleting them."""
    import inspect
    from app.research import exit_lab
    src = inspect.getsource(exit_lab)
    assert "FROM trades" in src
    for stmt in src.split("FROM trades")[1:3]:
        assert "mode" not in stmt[:160], \
            "exit_lab now filters by mode -- retired strategies can no longer be studied"
