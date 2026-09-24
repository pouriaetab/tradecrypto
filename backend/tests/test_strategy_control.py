"""The operator's switch. Every test here exists because the switch is the only
thing standing between a bad strategy and the book when nobody can edit code."""
import time
import pytest

from app.risk import control


# Every test here writes. `writable_db` (conftest) points TC_DB_PATH at a fresh
# throwaway file, so the suite never touches the live book -- the rule that
# exists because a test once wrote to the real append-only vault.
@pytest.fixture(autouse=True)
def _schema(writable_db):
    control.ensure_schema()
    yield


def test_unmanaged_strategy_is_open():
    """No row must mean 'nobody has said anything', never 'disabled'."""
    st = control.state("test_never_touched")
    assert st["enabled"] is True
    assert st["managed"] is False
    assert control.check("test_never_touched", 100.0, "paper") == []


def test_switch_off_blocks_entries():
    control.set_state("test_off", enabled=False, note="lost $14 in 3 hours")
    reasons = control.check("test_off", 50.0, "paper", intent="open")
    assert reasons and "switched off" in reasons[0]
    assert "lost $14 in 3 hours" in reasons[0]


def test_switch_off_NEVER_blocks_an_exit():
    """The whole safety story. A strategy that is off must still be able to sell.

    If this ever goes green while entries are blocked but exits are too, the
    desk can strand money in a position with nothing watching it, which is a
    worse failure than the one the switch exists to prevent.
    """
    control.set_state("test_off2", enabled=False)
    for intent in ("close", "exit", "reduce", "stop"):
        assert control.check("test_off2", 50.0, "paper", intent=intent) == []


def test_per_trade_cap():
    control.set_state("test_cap", max_position_usd=25.0)
    assert control.check("test_cap", 24.99, "paper") == []
    out = control.check("test_cap", 25.01, "paper")
    assert out and "per-trade cap" in out[0]


def test_zero_means_no_cap():
    control.set_state("test_zero", max_position_usd=0.0, daily_budget_usd=0.0)
    assert control.check("test_zero", 10_000.0, "paper") == []


def test_daily_budget_counts_filled_opens_only(monkeypatch):
    control.set_state("test_budget", daily_budget_usd=100.0)
    monkeypatch.setattr(control, "spent_today", lambda s, m: 80.0)
    assert control.check("test_budget", 19.0, "paper") == []
    out = control.check("test_budget", 21.0, "paper")
    assert out and "daily budget" in out[0]


def test_operator_outranks_auto():
    """An allocator must never be able to switch back on what the operator
    switched off. This is the rule that makes the switch trustworthy."""
    control.set_state("test_rank", enabled=False, source=control.SOURCE_OPERATOR)
    res = control.set_state("test_rank", enabled=True, source=control.SOURCE_AUTO)
    assert res.get("refused")
    assert control.state("test_rank")["enabled"] is False
    # ...but the operator can always change their own mind.
    control.set_state("test_rank", enabled=True, source=control.SOURCE_OPERATOR)
    assert control.state("test_rank")["enabled"] is True


def test_auto_may_manage_a_row_it_owns():
    control.set_state("test_auto", daily_budget_usd=50.0, source=control.SOURCE_AUTO)
    res = control.set_state("test_auto", daily_budget_usd=20.0, source=control.SOURCE_AUTO)
    assert not res.get("refused")
    assert control.state("test_auto")["daily_budget_usd"] == 20.0


def test_partial_update_leaves_other_fields_alone():
    control.set_state("test_part", enabled=False, daily_budget_usd=40.0,
                      max_position_usd=15.0, note="hi")
    control.set_state("test_part", enabled=True)
    st = control.state("test_part")
    assert st["enabled"] is True
    assert st["daily_budget_usd"] == 40.0
    assert st["max_position_usd"] == 15.0
    assert st["note"] == "hi"


def test_check_fails_open_when_the_table_is_unreadable(monkeypatch):
    """A control table that cannot be read must not stop the desk trading."""
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(control, "state", boom)
    assert control.check("anything", 50.0, "paper") == []


def test_none_strategy_is_not_blocked():
    assert control.check(None, 50.0, "paper") == []


def test_guards_actually_consult_the_switch():
    """The unit above proves control.check works. This proves it is WIRED --
    the failure mode that matters is a correct switch nobody asks."""
    import inspect
    from app.risk import guards
    src = inspect.getsource(guards.pre_trade_check)
    assert "control.check" in src or "_control.check" in src, \
        "pre_trade_check no longer consults the per-strategy switch"


def test_roster_keeps_a_strategy_that_left_the_active_list_but_still_holds_money():
    """The bug this file exists to stop repeating.

    burst_catch was taken off ACTIVE_STRATEGIES because it was being retired,
    and it vanished from the control card while it still held 17 trades, 4 open
    positions and -$21.63 of the book. The one strategy that needed the retire
    button was the one the retire button no longer listed.
    """
    from app.core import db
    db.execute(
        """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px,
                              ts_open, ts_close, holding_s, gross_pnl_usd,
                              cost_usd, net_pnl_usd)
           VALUES ('C','gone_from_list','paper',1.0,10.0,9.0,1.0,2.0,1.0,-0.5,0.5,-1.0)""")
    names = [r["strategy"] for r in control.roster("paper")]
    assert "gone_from_list" in names
    row = next(r for r in control.roster("paper") if r["strategy"] == "gone_from_list")
    assert row["on_active_roster"] is False
    assert row["standing"] == "off the roster — still in the book"


def test_roster_keeps_a_strategy_that_only_has_an_open_position():
    from app.core import db
    db.execute("""INSERT INTO positions(symbol, mode, strategy, qty, avg_px, opened_ts)
                  VALUES ('C','paper','only_open',1.0,10.0,1.0)""")
    assert "only_open" in [r["strategy"] for r in control.roster("paper")]


def test_roster_keeps_a_strategy_that_is_only_in_the_lab():
    from app.core import db
    db.execute(
        """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px,
                              ts_open, ts_close, holding_s, gross_pnl_usd,
                              cost_usd, net_pnl_usd)
           VALUES ('C','parked','lab',1.0,10.0,9.0,1.0,2.0,1.0,-0.5,0.5,-1.0)""")
    row = next(r for r in control.roster("paper") if r["strategy"] == "parked")
    assert row["standing"] == "in the lab"
    assert row["lab_trades"] == 1


def test_roster_survives_a_missing_control_table():
    """strategy_control does not exist until the first switch is thrown. A
    roster that throws there would leave the operator with no card at all."""
    from app.core import db
    db.execute("DROP TABLE IF EXISTS strategy_control")
    names = [r["strategy"] for r in control.roster("paper")]
    from app.strategy.registry import ACTIVE_STRATEGIES
    assert set(ACTIVE_STRATEGIES).issubset(set(names))


def test_every_active_strategy_is_always_listed():
    from app.strategy.registry import ACTIVE_STRATEGIES
    names = [r["strategy"] for r in control.roster("paper")]
    assert set(ACTIVE_STRATEGIES).issubset(set(names))
    assert len(names) == len(set(names)), "a strategy is listed twice"
