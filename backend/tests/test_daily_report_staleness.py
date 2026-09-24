"""A finished day's report must be rebuilt when something lands in it late.

2026-09-19: the Daily tab said Sep 18 was 7 trades / +$36.73. The Journal, which
reads the trades directly, said 8 / +$42.29. Both were reading the same database.
The report had been built at 21:20 Austin by a six-hourly job; ENA closed at
22:49, still on the 18th, and the day then stopped being "today" — so the report
was treated as final forever, 89 minutes short of the truth.

Two tabs, two answers, and the wrong one was the one that looked authoritative.
"""
from __future__ import annotations

import json
import time

import pytest


def _a_trade(db, ts_close, symbol="ENA", net=5.56):
    db.execute(
        "INSERT INTO trades (symbol, strategy, mode, qty, entry_px, exit_px, "
        "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "day_climb", "paper", 100.0, 0.18, 0.19,
         ts_close - 3600, ts_close, 3600, net + 2.1, 2.1, net))


class TestStaleReports:

    def test_a_late_trade_makes_the_day_stale(self, writable_db):
        from app.core import db
        from app.research import daily_report as dr
        day = "2026-09-18"
        t0, t1 = dr._bounds(day)
        # Report built mid-evening, then a trade closes an hour later.
        dr.ensure_schema()
        built = t1 - 2 * 3600
        db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?)",
                   (day, built, json.dumps({"day": day, "pnl": {"net_usd": 36.73}})))
        assert dr._last_activity(day) == 0.0        # nothing yet
        _a_trade(db, ts_close=t1 - 3600)            # lands AFTER the report
        assert dr._last_activity(day) > built, "the late trade is invisible to staleness"

    def test_the_report_is_rebuilt_rather_than_served_stale(self, writable_db, monkeypatch):
        from app.core import db
        from app.research import daily_report as dr
        day = "2026-09-18"
        t0, t1 = dr._bounds(day)
        dr.ensure_schema()
        db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?)",
                   (day, t1 - 2 * 3600, json.dumps({"day": day, "stale": True})))
        _a_trade(db, ts_close=t1 - 3600)
        rebuilt = {}
        monkeypatch.setattr(dr, "build",
                            lambda d: rebuilt.setdefault(d, {"day": d, "built_at": time.time()}))
        out = dr.get(day)
        assert day in rebuilt, "a finished day with a late trade was served from cache"
        assert not out.get("stale")

    def test_a_settled_day_is_still_served_from_cache(self, writable_db, monkeypatch):
        """The rebuild must be triggered by DATA, not by running at all — or every
        page load pays for a full rebuild of every day."""
        from app.core import db
        from app.research import daily_report as dr
        day = "2026-09-15"
        t0, t1 = dr._bounds(day)
        dr.ensure_schema()
        _a_trade(db, ts_close=t0 + 3600)            # a trade, early in the day
        # The stored row must carry the CURRENT report version, or it is stale for
        # a different and equally correct reason (2026-09-22: a row built by older
        # logic is rebuilt however quiet the day has been). This test is about the
        # DATA path, so it holds the version constant.
        db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?)",
                   (day, t1, json.dumps({"day": day, "cached": True,
                                         "report_version": dr.REPORT_VERSION})))  # built after it
        called = []
        monkeypatch.setattr(dr, "build", lambda d: called.append(d) or {"day": d, "built_at": 0})
        out = dr.get(day)
        assert called == [], "a settled day was rebuilt for no reason"
        assert out.get("cached") is True


class TestTheRangeViewToo:
    """The Daily tab's charts, totals and per-day table all come from the RANGE
    report, not from get(). The first staleness fix reached get() only, so the
    one place the operator actually looks kept serving the stale answer -- and
    he reported the same wrong number a second time, after being told it was
    fixed. A repair that reaches one caller is not a repair."""

    def test_the_range_refreshes_a_day_that_gained_a_trade(self, writable_db, monkeypatch):
        from app.core import db
        from app.research import daily_report as dr
        day = "2026-09-18"
        t0, t1 = dr._bounds(day)
        dr.ensure_schema()
        db.execute("INSERT INTO daily_reports(day, built_at, summary_json) VALUES (?,?,?)",
                   (day, t1 - 2 * 3600, json.dumps({"day": day, "stale": True})))
        _a_trade(db, ts_close=t1 - 3600)
        seen = []
        real_get = dr.get
        monkeypatch.setattr(dr, "get", lambda d, force=False: seen.append(d))
        monkeypatch.setattr(dr, "available_days", lambda *a, **k: [day])
        try:
            dr.series(day, day)
        except Exception:
            pass                      # the stub get() returns None; we only care that it was called
        assert day in seen, "the range view served a stale day without refreshing it"
