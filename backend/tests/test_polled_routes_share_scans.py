"""A route the UI polls never runs a full scan of the bars table on every call.

2026-09-21 10:10: /mode (one row) took 27 s, /health/report (no database)
took 24 s, one engine tick took 365 s. GET /system/threads showed why: the
one database lock was held by API threads running COUNT(*) and MIN/MAX(ts)
over the 3-million-row bars table -- /system/status and /setup, polled by
every open tab every few seconds -- 3,577 acquisitions and 354 s of
cumulative waiting in the first ninety seconds after a boot. The engine's
tick queued behind the same scans. Nobody needs a bar count younger than two
minutes; these tests pin that the scans are shared.
"""
from __future__ import annotations

import time

from app.core import db


def _count_scans(monkeypatch, module):
    calls = {"bars": 0}
    real = db.query_one

    def spy(sql, params=()):
        if "FROM bars" in sql:
            calls["bars"] += 1
        return real(sql, params)
    monkeypatch.setattr(db, "query_one", spy)
    return calls


def test_system_status_shares_its_bar_and_quote_scans(writable_db, monkeypatch):
    from app.api.v1 import routes
    db._MEMO.clear()
    calls = _count_scans(monkeypatch, routes)
    routes.system_status()
    routes.system_status()
    routes.system_status()
    assert calls["bars"] == 1, f"bars scanned {calls['bars']} times for 3 polls"


def test_setup_state_shares_its_two_full_scans(writable_db, monkeypatch):
    from app.api.v1 import routes
    from app.execution import rh_spread
    rh_spread.ensure_schema()
    db._MEMO.clear()
    calls = _count_scans(monkeypatch, routes)
    routes.setup_state()
    routes.setup_state()
    assert calls["bars"] == 2, f"expected COUNT + MIN/MAX once each, saw {calls['bars']}"


def test_the_cache_expires(writable_db, monkeypatch):
    db._MEMO.clear()
    n = {"q": 0}
    real = db.query_one

    def spy(sql, params=()):
        n["q"] += 1
        return real(sql, params)
    monkeypatch.setattr(db, "query_one", spy)
    db.query_one_cached("SELECT COUNT(*) c FROM bars", ttl_s=0.2)
    db.query_one_cached("SELECT COUNT(*) c FROM bars", ttl_s=0.2)
    assert n["q"] == 1
    time.sleep(0.25)
    db.query_one_cached("SELECT COUNT(*) c FROM bars", ttl_s=0.2)
    assert n["q"] == 2


def test_todays_report_is_not_rebuilt_on_every_request(writable_db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.research import daily_report as dr
    dr.ensure_schema()
    today = datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    builds = {"n": 0}
    real_build = dr.build

    def counting_build(day):
        builds["n"] += 1
        return real_build(day)
    monkeypatch.setattr(dr, "build", counting_build)
    dr.get(today)
    dr.get(today)
    dr.get(today)
    assert builds["n"] == 1, f"today's report was built {builds['n']} times for 3 requests"
    dr.get(today, force=True)
    assert builds["n"] == 2, "force must still rebuild"


def test_cost_observables_are_shared_across_callers(writable_db, monkeypatch):
    """/movers polled observables() + fit_coefficients() every 30 s; each reads
    every stored minute bar in its window under the lock (2026-09-21)."""
    from app.execution import symbol_cost
    symbol_cost._memo.clear()
    n = {"bars": 0}
    real = db.query

    def spy(sql, params=()):
        if "FROM bars" in sql:
            n["bars"] += 1
        return real(sql, params)
    monkeypatch.setattr(db, "query", spy)
    symbol_cost.observables()
    symbol_cost.observables()
    symbol_cost.fit_coefficients()
    symbol_cost.fit_coefficients()
    assert n["bars"] == 2, f"minute bars were read {n['bars']} times for 4 calls (expect 1 per window)"


def test_retrain_status_scans_the_calendar_once(writable_db, monkeypatch):
    """status() asked 'how many days of bars' once per strategy (7 scans of
    the bars table per poll of the Strategies tab, 2026-09-21)."""
    from app.research import retrain
    db._MEMO.clear()
    n = {"cal": 0}
    real = db.query_one

    def spy(sql, params=()):
        if "COUNT(DISTINCT date(ts" in sql:
            n["cal"] += 1
        return real(sql, params)
    monkeypatch.setattr(db, "query_one", spy)
    retrain.status()
    retrain.status()
    assert n["cal"] == 1, f"the calendar scan ran {n['cal']} times for 2 status calls"
