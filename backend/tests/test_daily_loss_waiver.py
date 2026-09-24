"""A release while the day's loss is still past the cap is the operator's
decision for that day, not a two-minute pause.

2026-09-23: $71 down against a $60 cap. Released nine times between 14:22 and
18:04; re-engaged within minutes each time because the next signal re-read the
same loss. The event log said "released by operator" and never why it would
not stick. Now the release records a waiver for THAT day only, the guard does
not re-arm for the daily-loss reason that day, and the next day starts clean.
"""
from __future__ import annotations

import time

from app.core import db
from app.core import mode as mode_mod
from app.risk import guards


def _book(realised_today: float, equity: float = 2000.0) -> None:
    mode_mod.set_equity(equity)
    db.execute("DELETE FROM positions"); db.execute("DELETE FROM trades"); db.execute("DELETE FROM equity_curve")
    db.execute("DELETE FROM app_state WHERE key=?", (guards._OVERRIDE_KEY,))
    now = time.time()
    db.execute("INSERT INTO equity_curve(ts, mode, equity, cash, positions_value, realised_pnl, unrealised_pnl) "
               "VALUES (?,?,?,?,?,?,?)", (now, "paper", equity, equity, 0.0, 0.0, 0.0))
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) VALUES ('X','day_climb','paper',1,1,1,?,?,60,0,0,?)",
               (now - 3600, now - 60, realised_today))


def _cap_check(d):
    return next(c for c in d.checks if c["check"] == "daily_loss_cap")


def test_a_loss_past_the_cap_engages_and_a_release_waives_it_for_today(writable_db):
    # Derived, not typed. This said -71.57 because the cap was 3% of $2,000 on
    # the day it was written; raising the cap to 8% made the test assert a
    # Tuesday instead of a rule. Whatever the cap is, go past it.
    _book(-(guards.daily_loss_limit_usd() * 1.2 + 10.0))
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert not d.allowed and not _cap_check(d)["passed"]
    assert guards.get_settings().kill_switch_file.exists(), "the cap must still engage the switch"

    guards.release_kill_switch()
    assert not guards.get_settings().kill_switch_file.exists()
    assert guards.daily_loss_cap_waived_today()

    d2 = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert _cap_check(d2)["passed"], "the waived cap must not refuse the order"
    assert "waived" in _cap_check(d2)["note"]
    assert not guards.get_settings().kill_switch_file.exists(), "the switch must not re-arm for the waived cap"
    assert guards.status("paper")["daily_loss_cap_waived_today"] is True


def test_the_waiver_dies_at_midnight(writable_db, monkeypatch):
    _book(-(guards.daily_loss_limit_usd() * 1.2 + 10.0))
    guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    guards.release_kill_switch()
    assert guards.daily_loss_cap_waived_today()
    # Tomorrow: the key still says yesterday's date, so it no longer applies.
    monkeypatch.setattr(guards, "_today_key", lambda: "2099-01-01")
    assert not guards.daily_loss_cap_waived_today()
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert not _cap_check(d)["passed"]


def test_a_release_inside_the_cap_waives_nothing(writable_db):
    _book(-(guards.daily_loss_limit_usd() * 0.1))   # comfortably inside the cap
    guards.engage_kill_switch("operator pressed it")
    guards.release_kill_switch()
    assert not guards.daily_loss_cap_waived_today()
    assert not guards.get_settings().kill_switch_file.exists()


def test_the_drawdown_guard_still_engages_under_a_waiver(writable_db, monkeypatch):
    _book(-(guards.daily_loss_limit_usd() * 1.2 + 10.0))
    guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    guards.release_kill_switch()
    monkeypatch.setattr(guards, "current_drawdown_pct", lambda mode: 12.0)
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert not d.allowed
    assert guards.get_settings().kill_switch_file.exists()
    assert "drawdown" in guards.get_settings().kill_switch_file.read_text()
