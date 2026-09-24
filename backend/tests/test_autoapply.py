"""The desk picks up new code by itself — but only when it is safe to.

    "my ideal scenario is to never close or open stop or restart and have it
     all automated as much as possible"

Python loads its modules once, so every backend change sat inert until someone
remembered to restart. Two pieces already existed and were never connected:
`code_version` knows the source is newer than the running build, and
`supervise.sh` already treats exit code 3 as a planned restart.

The whole risk of connecting them is restarting at the wrong moment. These tests
are that risk, written down.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import autoapply


def _status(stale=True, boot_age=9999.0, edit_age=9999.0):
    now = time.time()
    return {
        "booted_at": now - boot_age,
        "newest_source_now": now - edit_age,
        "newest_source_file": "backend/app/execution/engine.py",
        "code_changed_since_boot": stale,
    }


@pytest.fixture
def stub(monkeypatch):
    """Drive check() entirely from the test; nothing touches the real clock or db."""
    def _set(stale=True, boot_age=9999.0, edit_age=9999.0, in_flight=0, on=True):
        monkeypatch.setattr(autoapply.code_version, "status",
                            lambda: _status(stale, boot_age, edit_age))
        monkeypatch.setattr(autoapply, "orders_in_flight", lambda: in_flight)
        monkeypatch.setenv("TC_AUTO_APPLY_CODE", "1" if on else "0")
    return _set


class TestItRestartsOnlyWhenSafe:

    def test_it_restarts_when_the_code_is_new_and_nothing_is_happening(self, stub):
        stub()
        d = autoapply.check()
        assert d["restart"] is True and "new backend code" in d["why"]

    def test_an_order_in_flight_stops_it_dead(self, stub):
        """THE ONE THAT MATTERS. A restart between 'submitted' and 'filled' is how
        you lose track of real money."""
        stub(in_flight=1)
        d = autoapply.check()
        assert d["restart"] is False
        assert "in flight" in d["why"]

    def test_it_waits_for_edits_to_settle(self, stub):
        """A file saved four seconds ago is probably one of several being saved.
        Restarting mid-edit runs half a change."""
        stub(edit_age=5.0)
        d = autoapply.check()
        assert d["restart"] is False and "settle" in d["why"]

    def test_it_will_not_restart_a_process_that_just_started(self, stub):
        """Without this, a clock skew or an odd mtime restarts the desk in a loop."""
        stub(boot_age=5.0)
        d = autoapply.check()
        assert d["restart"] is False and "up" in d["why"]

    def test_unchanged_code_is_never_a_reason_to_restart(self, stub):
        stub(stale=False)
        assert autoapply.check()["restart"] is False

    def test_it_can_be_turned_off(self, stub):
        stub(on=False)
        d = autoapply.check()
        assert d["restart"] is False and "off" in d["why"]

    def test_an_unreadable_database_counts_as_busy(self, monkeypatch):
        """If we cannot tell whether an order is in flight, we do not restart."""
        from app.core import db
        def boom(*a, **k):
            raise RuntimeError("database unavailable")
        monkeypatch.setattr(db, "query_one", boom)
        assert autoapply.orders_in_flight() == 1


class TestItIsWiredIn:

    def test_the_engine_loop_actually_calls_it(self):
        """A mechanism only the tests exercise is the 5.3 failure — a fix that
        cannot reach production."""
        src = (Path(__file__).resolve().parents[1] / "app" / "execution" / "engine.py").read_text()
        assert "autoapply" in src and "apply_now" in src

    def test_a_planned_restart_does_not_sit_out_a_backoff(self):
        """Every second here is a second of live quotes that cannot be recovered.

        Bars are refetched from the exchange by minute_topup; the live mid is
        stored nowhere else, so a gap in it is permanent. A backoff exists to
        stop a BROKEN build spinning all night -- exit 6 is the process asking
        to be restarted having already checked nothing is in flight, so there is
        nothing to back off from. Measured on 2026-09-19: the pause was 5 of the
        8 seconds every code update cost.
        """
        import re
        sup = (Path(__file__).resolve().parents[2] / "scripts" / "supervise.sh").read_text()
        blk = sup.split("\n    6)", 1)
        assert len(blk) == 2, "supervise.sh no longer handles exit code 6"
        blk = blk[1].split(";;", 1)[0]
        assert re.search(r"\bbackoff=0\b", blk), \
            "a planned code restart still sits out a backoff before restarting"
        # ...and a real crash still does, or a broken build spins forever.
        crash = sup.split("\n    3)", 1)[1].split(";;", 1)[0]
        assert "MIN_BACKOFF" in crash, "a memory-ceiling restart lost its backoff"

    def test_it_exits_with_the_code_the_supervisor_treats_as_planned(self):
        """supervise.sh exit codes: 3 memory ceiling, 4 port in use, 5 TCC,
        6 new code. All of 3 and 6 restart with no backoff and no failed-start
        count; any other code is read as a crash and backed off.

        6, not 3: sharing one code made the supervisor log "memory-ceiling
        restart" every time the desk had merely picked up an edit, which is a
        log that lies about the one thing it exists to record.
        """
        import re
        assert autoapply.RESTART_EXIT_CODE == 6
        sup = (Path(__file__).resolve().parents[2] / "scripts" / "supervise.sh").read_text()
        block = sup.split("\n    6)", 1)
        assert len(block) == 2, "supervise.sh does not handle exit code 6"
        block = block[1].split(";;", 1)[0]
        # What it SAYS, not what its comments mention.
        said = re.findall(r'say "([^"]*)"', block)
        assert said, "exit 6 is handled silently"
        assert not any("memory" in m.lower() for m in said), \
            f"exit 6 still reports a memory-ceiling restart: {said}"
        assert any("code" in m.lower() for m in said), \
            f"exit 6 does not say it picked up new code: {said}"
        # Handled as PLANNED: the fast-failure counter resets, so nothing
        # escalates. How long it waits is the next test's business -- exit 6
        # waits not at all, because the process asked for this restart.
        assert "consecutive_fast_failures=0" in block
        assert "backoff=" in block


class TestTheDeskKnowsWhenItWasNotRunning:
    """The desk needs no browser — it is a thread on its own clock — but it does
    need the Mac awake. A closed lid stops everything, and on waking the loop
    carries on as though nothing happened, so hours of missing bars and signals
    look exactly like hours of quiet market."""

    def test_a_long_gap_between_ticks_is_recorded_not_ignored(self):
        src = (Path(__file__).resolve().parents[1] / "app" / "execution" / "engine.py").read_text()
        assert "desk_gap" in src, "a sleep/stop gap must reach the journal"
        assert "last_tick_wall" in src
        # It must key off the poll interval, not a hardcoded number of minutes:
        # the interval is configurable and a fixed threshold would drift from it.
        assert "poll_interval_s" in src.split("desk_gap")[0].rsplit("DID THE MACHINE SLEEP", 1)[-1]

    def test_the_installer_offers_a_stay_awake_mode(self):
        sh = (Path(__file__).resolve().parents[2] / "scripts" / "install-autostart.sh").read_text()
        assert "--awake" in sh and "caffeinate" in sh
        # and it must say what the default costs, or nobody learns the machine
        # sleeping is what ended their 24/7
        assert "sleep" in sh.lower()


def test_an_env_edit_counts_as_new_code(tmp_path, monkeypatch):
    """2026-09-23: TC_MAX_DAILY_LOSS_PCT raised at 16:51, still $60 at 18:04.
    .env is read once at boot; an edit to it must trigger the same planned
    restart a .py edit does, or the operator's setting silently never applies."""
    import os
    import time as _t
    from app.core import code_version as cv
    root = tmp_path
    (root / "backend" / "app").mkdir(parents=True)
    py = root / "backend" / "app" / "x.py"; py.write_text("x = 1\n")
    old = _t.time() - 3600
    os.utime(py, (old, old))
    monkeypatch.setattr(cv, "_ROOT", root)
    newest, where = cv._newest()
    assert where.endswith("x.py")
    env = root / ".env"; env.write_text("TC_MAX_DAILY_LOSS_PCT=8.0\n")
    newest2, where2 = cv._newest()
    assert where2 == ".env" and newest2 > newest
