"""A shipped fix has to reach rows that are already on disk — and the Restart
button has to say what it actually did.

2026-09-22, both found in the same five minutes:

  * `daily_report` was scoped to the running mode, so retired trades stopped
    being counted into the paper day. The stored rows kept their wrong numbers,
    because staleness asked "did anything happen that day since it was built?"
    — a question about the DATA that says nothing about the CODE. On a finished
    day the answer is never yes.
  * The dashboard's Restart button exited with `health.MEMORY_EXIT_CODE`, so
    every deliberate restart was logged by the supervisor as a memory-ceiling
    event and paid its 5-second backoff.
"""
import inspect
import json

import pytest

from app.core import db
from app.research import daily_report


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    daily_report.ensure_schema()
    yield


def _store(day, version, built_at=10_000_000_000.0):
    """A stored report from some version of the code, on a day with no later activity."""
    payload = {"day": day,
               "counts": {"worth_taking": 0, "uncovered": 0, "missed": 0,
                          "traded": 0, "trades_closed": 40},
               "opportunity": {"best_available_pct": 0.0},
               "pnl": {"net_usd": -42.65, "gross_usd": 1.46, "cost_usd": 44.11,
                       "equity_end": None}}
    if version is not None:
        payload["report_version"] = version
    db.execute(
        "INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?) "
        "ON CONFLICT(day) DO UPDATE SET built_at=excluded.built_at, "
        "summary_json=excluded.summary_json",
        (day, built_at, json.dumps(payload)))


def _rebuilt(day, monkeypatch):
    """Did the REAL get() rebuild this day? Goes through the shipped code path
    rather than reimplementing the decision -- a test that re-derives the rule it
    is checking stays green when the rule is deleted."""
    calls = {}
    monkeypatch.setattr(daily_report, "build",
                        lambda d: calls.setdefault(d, {"day": d, "fresh": True,
                                                       "built_at": 10_000_000_001.0,
                                                       "report_version": daily_report.REPORT_VERSION}))
    daily_report.get(day)
    return day in calls


def test_a_row_from_an_older_version_is_rebuilt(monkeypatch):
    """THE REGRESSION: the fix shipped and the operator still saw the old number."""
    _store("2026-09-01", daily_report.REPORT_VERSION - 1)
    assert _rebuilt("2026-09-01", monkeypatch)


def test_a_row_with_no_version_at_all_is_rebuilt(monkeypatch):
    _store("2026-09-02", None)
    assert _rebuilt("2026-09-02", monkeypatch)


def test_a_row_from_the_current_version_is_left_alone(monkeypatch):
    """The other half — or every page view rebuilds every day, forever."""
    _store("2026-09-03", daily_report.REPORT_VERSION)
    assert not _rebuilt("2026-09-03", monkeypatch)


def test_a_corrupt_row_is_rebuilt_rather_than_trusted(monkeypatch):
    db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?)",
               ("2026-09-04", 10_000_000_000.0, "{not json"))
    assert _rebuilt("2026-09-04", monkeypatch)


def test_the_version_is_actually_written_into_the_summary():
    """A stamp nothing writes makes every row look ancient forever."""
    src = inspect.getsource(daily_report.build)
    assert '"report_version"' in src, "build() does not stamp the version it produced"


def test_the_version_was_bumped_for_the_mode_fix():
    assert daily_report.REPORT_VERSION >= 2, (
        "the mode-scoping fix needs a version bump, or rows built before it "
        "keep their wrong numbers")


def test_restart_uses_the_planned_code_not_the_memory_code():
    """These were deliberately split on 2026-09-19. The Restart button kept
    using the memory one, so it was logged as a memory event and waited 5s."""
    from app.core import setup_ops, autoapply, health
    src = inspect.getsource(setup_ops.restart)
    assert "autoapply.RESTART_EXIT_CODE" in src
    # The docstring names the old code to explain the history; what must not
    # use it is the line that actually exits.
    exits = [ln for ln in src.splitlines() if "os._exit(" in ln]
    assert exits, "restart() no longer exits"
    assert all("MEMORY_EXIT_CODE" not in ln for ln in exits), exits
    assert autoapply.RESTART_EXIT_CODE != health.MEMORY_EXIT_CODE, \
        "the two codes are the same again — one number doing two jobs"


def test_the_supervisor_treats_the_planned_code_as_no_backoff():
    from app.core import autoapply
    sup = (ROOT := __import__("pathlib").Path(__file__).resolve().parent.parent.parent)
    text = (sup / "scripts" / "supervise.sh").read_text()
    assert f"\n    {autoapply.RESTART_EXIT_CODE})" in text, \
        "supervise.sh has no case for the planned-restart exit code"


# ── the two paths must agree ─────────────────────────────────────────────────
# The Daily tab reads BOTH /report/day (get) and /report/range (series). On
# 2026-09-22 get() learned that a row built by older code is stale, and series()
# -- which carried its own copy of the staleness rule -- did not. So the
# single-day view showed the corrected -$18.41 and the range, which the tab's
# totals are built from, kept serving -$42.65. The operator restarted three
# times. The function's own comment already said "a repair that only reaches one
# caller is not a repair"; the duplication had been left in anyway.

def test_series_rebuilds_an_old_version_row_exactly_like_get(monkeypatch):
    """THE REGRESSION. Two readers, one decision."""
    day = "2026-09-05"
    _store(day, daily_report.REPORT_VERSION - 1)
    calls = []
    monkeypatch.setattr(daily_report, "build", lambda d: (
        calls.append(d),
        {"day": d, "built_at": 10_000_000_001.0,
         "report_version": daily_report.REPORT_VERSION,
         "counts": {"worth_taking": 0, "uncovered": 0, "missed": 0, "traded": 0,
                    "trades_closed": 0},
         "opportunity": {"best_available_pct": 0.0},
         "pnl": {"net_usd": -18.41, "gross_usd": 4.13, "cost_usd": 22.54,
                 "equity_end": None}})[1])
    daily_report.series(day, day)
    assert calls == [day], "the range path served a row built by older code"


def test_series_does_not_carry_its_own_staleness_rule():
    """Structural: the moment it decides for itself, it can disagree again."""
    import inspect
    src = inspect.getsource(daily_report.series)
    assert "_last_activity" not in src, (
        "series() is deciding staleness itself again -- it must ask get()")
    assert "get(day)" in src


def test_both_readers_return_the_same_money(monkeypatch):
    day = "2026-09-06"
    _store(day, daily_report.REPORT_VERSION - 1)
    built = {"day": day, "built_at": 10_000_000_001.0,
             "report_version": daily_report.REPORT_VERSION,
             "counts": {"worth_taking": 0, "uncovered": 0, "missed": 0, "traded": 0,
                        "trades_closed": 19},
             "opportunity": {"best_available_pct": 0.0},
             "pnl": {"net_usd": -18.41, "gross_usd": 4.13, "cost_usd": 22.54,
                     "equity_end": None}}
    def _fake_build(d):
        # the real build() stores the row it produced; the stub must too, or
        # series() reads the old one back and the test measures nothing
        db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?) "
                   "ON CONFLICT(day) DO UPDATE SET built_at=excluded.built_at, "
                   "summary_json=excluded.summary_json",
                   (d, built["built_at"], json.dumps(built)))
        return built
    monkeypatch.setattr(daily_report, "build", _fake_build)
    one = daily_report.get(day)["pnl"]["net_usd"]
    rng = daily_report.series(day, day)
    row = next(r for r in rng["rows"] if r["day"] == day)
    assert one == row["net_usd"], f"single-day says {one}, range says {row['net_usd']}"
    # And the range's TOTAL -- the figure on the Daily tab -- must agree too.
    assert rng["totals"]["net_usd"] == pytest.approx(one)


def test_todays_throttle_does_not_serve_a_row_known_to_be_wrong(monkeypatch):
    """The throttle stops a polling page rebuilding every request. It must not
    also hand back a row already established as stale -- a throttle may delay
    work, it may not serve a wrong answer."""
    import datetime as _dt
    from zoneinfo import ZoneInfo as _Z
    today = _dt.datetime.now(_Z("America/Chicago")).strftime("%Y-%m-%d")
    import time as _t
    _store(today, daily_report.REPORT_VERSION - 1, built_at=_t.time() - 5)  # well inside the throttle
    assert _rebuilt(today, monkeypatch), \
        "today's throttle served a report built by older code"


def test_backfill_rebuilds_an_old_version_row_too(monkeypatch):
    """The THIRD copy of the staleness decision. backfill() pre-filtered which
    days were worth building using only "did anything happen since?" -- so a day
    whose meaning had changed was skipped by the scheduled job as well."""
    day = "2026-09-07"
    _store(day, daily_report.REPORT_VERSION - 1)
    monkeypatch.setattr(daily_report, "available_days", lambda n=60: [day])
    calls = []
    monkeypatch.setattr(daily_report, "build", lambda d: (
        calls.append(d),
        {"day": d, "built_at": 10_000_000_002.0,
         "report_version": daily_report.REPORT_VERSION,
         "counts": {"worth_taking": 0, "uncovered": 0, "missed": 0, "traded": 0,
                    "trades_closed": 0},
         "opportunity": {"best_available_pct": 0.0},
         "pnl": {"net_usd": -18.41, "gross_usd": 4.13, "cost_usd": 22.54,
                 "equity_end": None}})[1])
    daily_report.backfill(max_days=5)
    assert calls == [day], "the scheduled backfill skipped a day built by older code"


def test_only_one_place_decides_staleness():
    """Three readers had three copies of the rule and they disagreed. The rule
    lives in get(); everything else asks it."""
    import inspect
    for fn in (daily_report.series, daily_report.backfill):
        src = inspect.getsource(fn)
        assert "_last_activity" not in src, (
            f"{fn.__name__} is deciding staleness itself again")
    assert "_last_activity" in inspect.getsource(daily_report.get)
