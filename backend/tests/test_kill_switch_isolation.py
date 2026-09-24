"""A test that trips the kill switch trips ITS switch, never the desk's.

2026-09-21 08:37: test_a_loss_already_taken_today_shrinks_the_headroom set up a
$150 loss in a private temp database and called pre_trade_check. The guard did
what it is for -- crossed the cap, engaged the kill switch -- and wrote the
file at the hardcoded project path data/KILL_SWITCH, through the mount, on the
running desk. The desk opened nothing for the rest of the morning. Its own
realised P&L that day was +$7. The CRITICAL event went into the temp database,
so the live log never explained it.

This test does the same thing on purpose and checks where the file landed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from app.config import PROJECT_ROOT, get_settings
from app.core import db
from app.core import mode as mode_mod
from app.risk import guards


def _snapshot(p: Path):
    return (p.exists(), p.stat().st_mtime if p.exists() else None)


def test_the_suite_points_the_switch_outside_the_project():
    ks = get_settings().kill_switch_file
    assert PROJECT_ROOT.resolve() not in ks.resolve().parents, ks
    assert ks == Path(os.environ["TC_KILL_SWITCH_FILE"]).resolve()


def test_tripping_the_cap_in_a_test_never_touches_the_desk(writable_db):
    real = PROJECT_ROOT / "data" / "KILL_SWITCH"
    before = _snapshot(real)
    mine = get_settings().kill_switch_file
    assert not mine.exists()

    now = time.time()
    mode_mod.set_equity(2000.0)
    db.execute("INSERT INTO equity_curve(ts, mode, equity, cash, positions_value, realised_pnl, unrealised_pnl) "
               "VALUES (?,?,?,?,?,?,?)", (now, "paper", 2000.0, 2000.0, 0.0, 0.0, 0.0))
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) VALUES ('LOSS','day_climb','paper',1,1,1,?,?,60,0,0,?)",
               # Derived from the cap in force, not typed. -150.0 cleared a 3%
               # cap on $2,000 ($60) and stopped clearing it the day the cap
               # moved to 8% ($160) -- the test then proved nothing while still
               # looking like it did.
               (now - 3600, now - 60, -(guards.daily_loss_limit_usd() * 1.2 + 10.0)))
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert not d.allowed
    assert mine.exists(), "the guard must still engage the switch it was given"
    assert "daily loss cap" in mine.read_text()
    assert _snapshot(real) == before, "the desk's own kill switch changed during a test"
