"""Retiring a strategy: out of the book, still in the lab, and reversible.

The operator's instruction was "remove all of the trades made by burst_catch
from the paper trading... but keep them to train on". Those two halves pull in
opposite directions, and every test here pins one of them down.
"""
import pytest

from app.core import db
from app.research import retire


def _seed(strategy, mode="paper", n=3, net=-2.0, with_open=False):
    for i in range(n):
        db.execute(
            """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px,
                                  ts_open, ts_close, holding_s, gross_pnl_usd,
                                  cost_usd, net_pnl_usd)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"C{i}", strategy, mode, 1.0, 10.0, 10.0 + net,
             1000.0 + i, 2000.0 + i, 1000.0, net + 1.0, 1.0, net))
    if with_open:
        db.execute(
            """INSERT INTO positions(symbol, mode, strategy, qty, avg_px, opened_ts)
               VALUES (?,?,?,?,?,?)""", ("OPENC", mode, strategy, 5.0, 3.0, 1500.0))


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def test_preview_changes_nothing():
    _seed("bad", n=4, net=-3.0)
    before = retire.preview("bad")
    retire.preview("bad")
    assert before["closed_trades"] == 4
    assert db.query_one("SELECT COUNT(*) n FROM trades WHERE mode='paper'")["n"] == 4


def test_preview_states_the_book_before_and_after():
    _seed("bad", n=2, net=-5.0)      # -10
    _seed("good", n=2, net=+4.0)     # +8
    p = retire.preview("bad")
    assert p["net_usd"] == pytest.approx(-10.0)
    assert p["book_net_before_usd"] == pytest.approx(-2.0)
    assert p["book_net_after_usd"] == pytest.approx(8.0)


def test_retire_removes_it_from_the_book_and_leaves_everything_else():
    _seed("bad", n=3, net=-7.0)
    _seed("good", n=2, net=+4.0)
    res = retire.retire("bad", "lost money fast")
    assert res["ok"]
    paper = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usd),0) net "
                         "FROM trades WHERE mode='paper'")
    assert paper["n"] == 2
    assert paper["net"] == pytest.approx(8.0)


def test_the_trades_still_exist_for_the_lab():
    """The whole point. A delete would satisfy the first half and destroy the
    second -- these 17 real fills are the only first-day evidence there is."""
    _seed("bad", n=3, net=-7.0)
    retire.retire("bad", "lost money fast")
    assert db.query_one("SELECT COUNT(*) n FROM trades WHERE strategy='bad'")["n"] == 3
    lab = db.query_one("SELECT COUNT(*) n FROM trades WHERE mode=?", (retire.LAB_MODE,))
    assert lab["n"] == 3


def test_exit_lab_still_sees_retired_trades():
    """exit_lab reads `FROM trades` with no mode filter. If that ever changes,
    retiring would silently become deleting, so it is asserted here."""
    import inspect
    from app.research import exit_lab
    src = inspect.getsource(exit_lab)
    assert "FROM trades" in src
    for line in src.splitlines():
        if "FROM trades" in line:
            assert "mode" not in line.lower(), \
                "exit_lab now filters trades by mode -- retiring would hide them from the lab"


def test_retire_switches_the_strategy_off_too():
    from app.risk import control
    _seed("bad", n=2, net=-1.0)
    retire.retire("bad", "no edge")
    assert control.state("bad")["enabled"] is False


def test_restore_is_the_exact_inverse():
    _seed("bad", n=3, net=-7.0)
    _seed("good", n=2, net=+4.0)
    before = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usd),0) net "
                          "FROM trades WHERE mode='paper'")
    retire.retire("bad", "x")
    retire.restore("bad")
    after = db.query_one("SELECT COUNT(*) n, COALESCE(SUM(net_pnl_usd),0) net "
                         "FROM trades WHERE mode='paper'")
    assert after["n"] == before["n"]
    assert after["net"] == pytest.approx(before["net"])


def test_orders_and_positions_move_with_the_trades():
    _seed("bad", n=1, net=-1.0)
    db.execute("""INSERT INTO orders(client_id, strategy, symbol, side, intent, qty,
                                     notional_usd, mode, status, ts_decided)
                  VALUES ('c1','bad','C0','buy','open',1.0,50.0,'paper','filled',1000.0)""")
    retire.retire("bad", "x", close_open_first=False)
    assert db.query_one("SELECT COUNT(*) n FROM orders WHERE mode='paper'")["n"] == 0
    assert db.query_one("SELECT COUNT(*) n FROM orders WHERE mode=?",
                        (retire.LAB_MODE,))["n"] == 1


def test_an_open_position_is_never_moved_while_open():
    """A position in a mode nothing marks is a position with nothing watching
    it -- the exact failure the operator's switch exists to prevent."""
    _seed("bad", n=1, net=-1.0, with_open=True)
    res = retire.retire("bad", "x")          # no universe row -> cannot quote it
    assert res["ok"] is False
    assert "could not close" in res["error"]
    # and NOTHING moved
    assert db.query_one("SELECT COUNT(*) n FROM trades WHERE mode='paper'")["n"] == 1
    assert db.query_one("SELECT COUNT(*) n FROM positions WHERE mode='paper'")["n"] == 1


def test_retired_lists_what_is_in_the_lab():
    _seed("bad", n=3, net=-7.0)
    retire.retire("bad", "x")
    rows = retire.retired()
    assert [r["strategy"] for r in rows] == ["bad"]
    assert rows[0]["closed_trades"] == 3
    assert rows[0]["net_usd"] == pytest.approx(-21.0)


def test_burst_catch_is_off_the_active_roster_but_still_constructible():
    from app.strategy.registry import ACTIVE_STRATEGIES, RETIRED, STRATEGIES
    assert "burst_catch" not in ACTIVE_STRATEGIES
    assert "burst_catch" in RETIRED
    assert "burst_catch" in STRATEGIES, "the lab still has to be able to build it"
