"""One definition of "today", and it is not the machine's timezone.

2026-09-22: every day-by-day table the assistant produced from a UTC machine was
shifted five hours, because `date(ts,'unixepoch','localtime')` means "the
timezone of whatever runs this". The app was right (daily_report and guards both
go through core/clock); the ad-hoc SQL beside it was not.
"""
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.core import clock

ROOT = Path(__file__).resolve().parent.parent.parent


def test_day_key_is_chicago_not_the_machine():
    """19:30 Chicago is 00:30 UTC the NEXT day. The trading day must say the
    earlier date whatever TZ the process is in."""
    ts = dt.datetime(2026, 9, 21, 19, 30, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert clock.day_key(ts) == "2026-09-21"
    assert dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") == "2026-09-22"


def test_day_key_survives_a_utc_process():
    """The assistant's sandbox is Etc/UTC. That must not change the answer."""
    saved = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Etc/UTC"
        try:
            import time as _t
            _t.tzset()
        except AttributeError:
            pass
        ts = dt.datetime(2026, 9, 21, 22, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()
        assert clock.day_key(ts) == "2026-09-21"
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        try:
            import time as _t
            _t.tzset()
        except AttributeError:
            pass


def test_bounds_for_day_is_24h_and_starts_at_local_midnight():
    t0, t1 = clock.bounds_for_day("2026-09-21")
    assert dt.datetime.fromtimestamp(t0, ZoneInfo("America/Chicago")).hour == 0
    assert 23 * 3600 <= (t1 - t0) <= 25 * 3600, "a day, allowing for daylight time"


def test_bounds_and_day_key_agree():
    t0, t1 = clock.bounds_for_day("2026-09-21")
    assert clock.day_key(t0) == "2026-09-21"
    assert clock.day_key(t1 - 1) == "2026-09-21"
    assert clock.day_key(t1) == "2026-09-22"


def test_a_dst_day_is_not_assumed_to_be_24_hours():
    """Chicago is UTC-5 in summer and UTC-6 in winter. A hardcoded offset --
    which this repo has been bitten by before -- breaks on the changeover."""
    summer = dt.datetime(2026, 7, 1, 12, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
    winter = dt.datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("America/Chicago")).utcoffset()
    assert summer != winter
    for day in ("2026-03-08", "2026-11-01"):
        t0, t1 = clock.bounds_for_day(day)
        assert clock.day_key(t0) == day
        assert clock.day_key(t1 - 1) == day


def test_the_checker_is_wired_and_passing():
    """The rule only protects anything if it actually runs."""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_day_boundary.py")],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
    gate = (ROOT / "scripts" / "gate.sh").read_text()
    assert "check_day_boundary.py" in gate, "the checker is not in the gate"


def test_sizing_buckets_fills_in_the_operators_day():
    """fills_per_day sets every position's risk budget. If it counts UTC days,
    an evening's trades land on tomorrow and the median is wrong."""
    import inspect
    from app.feedback import sizing
    src = inspect.getsource(sizing.fills_per_day)
    # The word appears in the comment explaining why it is NOT used; what must
    # not appear is the SQL modifier itself.
    assert "'localtime'" not in src and '"localtime"' not in src, \
        "fills_per_day is back on the machine's timezone"
    assert "clock.day_key" in src


def test_the_frontend_has_an_undefined_identifier_check():
    """2026-09-22: the Risk tab died with "key is not defined" the moment the
    operator asked a table for a total — a parameter renamed in the signature and
    left behind in the body. Neither render check saw it: they render the page at
    rest, and the stats are computed on a click.

    That is three bugs in the same family now (a branch no check enters), so the
    answer is a check that does not care about branches. This asserts the rule
    exists and that the gate runs it — a lint config nothing invokes is decoration.
    """
    cfg = ROOT / "frontend" / "eslint.config.mjs"
    assert cfg.exists(), "the frontend lint config is gone"
    text = cfg.read_text()
    assert "'no-undef': 'error'" in text, "no-undef is no longer an error"
    gate = (ROOT / "scripts" / "gate.sh").read_text()
    assert "eslint.config.mjs" in gate, "the gate does not run the lint config"
    assert "no-undef" in gate
