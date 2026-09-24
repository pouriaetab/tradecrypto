"""Tests for the things that killed the process overnight.

A leak is invisible until it is fatal, so it gets a test that fails loudly the
moment a cache stops being bounded.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import health, housekeeping     # noqa: E402
from app.research import breakout as bo       # noqa: E402


def test_level_cache_is_hard_bounded():
    """The original bug: veto() wrote to a plain dict that nothing ever cleared.
    Overnight that is >100k entries of Level lists, and the process dies."""
    bo._LEVEL_CACHE.clear()
    for i in range(bo._LEVEL_CACHE_MAX * 4):
        bo._cache_put(("SYM", i), [bo.Level(price=float(i), kind="pivot_high")])
    assert len(bo._LEVEL_CACHE) == bo._LEVEL_CACHE_MAX
    bo._LEVEL_CACHE.clear()


def test_level_cache_evicts_oldest_first():
    bo._LEVEL_CACHE.clear()
    for i in range(bo._LEVEL_CACHE_MAX + 10):
        bo._cache_put(("S", i), [])
    keys = list(bo._LEVEL_CACHE)
    assert ("S", 0) not in keys           # oldest gone
    assert ("S", bo._LEVEL_CACHE_MAX + 9) in keys   # newest kept
    bo._LEVEL_CACHE.clear()


def test_memory_report_is_sane():
    m = health.memory()
    assert m["rss_mb"] > 0
    assert m["ceiling_mb"] > m["rss_mb"], "the test process should be far below the ceiling"
    assert m["peak_mb"] >= m["rss_mb"]


def test_disk_report_has_free_space():
    d = health.disk()
    assert d["free_gb"] is None or d["free_gb"] >= 0
    assert d["project_mb"] >= 0
    # low_space must be a real boolean, never None, since a banner keys off it
    assert isinstance(d["low_space"], bool)


def test_housekeeping_dry_run_deletes_nothing():
    before = health.disk()["project_mb"]
    r = housekeeping.run(dry_run=True)
    assert r["dry_run"] is True
    assert all(a.get("deleted") is not True for a in r["actions"])
    assert abs(health.disk()["project_mb"] - before) < 1.0


def test_retention_keeps_the_history_research_depends_on():
    """Hourly and daily bars must never be in a deletion plan — they are the
    four years everything is validated against."""
    r = housekeeping.run(dry_run=True)
    labels = " ".join(a["table"] for a in r["actions"])
    assert "hourly" not in labels.lower()
    assert "daily" not in labels.lower()
    assert r["retention"]["hourly_and_daily"].startswith("kept forever")


def test_memory_exit_code_is_distinct():
    """Exit code 3 has to be distinguishable from a crash so the supervisor can
    treat it as a planned restart rather than a failure loop."""
    assert health.MEMORY_EXIT_CODE == 3
    assert health.MEMORY_EXIT_CODE not in (0, 1, 2, 130, 143)


def test_tick_profile_ring_is_hard_bounded():
    """Invariant 23: the tick profiler keeps a ring, never a list."""
    from app.execution import engine
    engine._profile.clear()
    for i in range(engine._PROFILE_N * 3):
        engine._profile.append({"total": 0.01, "positions": 0.005})
    assert len(engine._profile) == engine._PROFILE_N
    p = engine.tick_profile()
    assert p["n"] == engine._PROFILE_N
    assert abs(p["phases"]["positions"]["share_pct"] - 50.0) < 1e-9
    engine._profile.clear()
