"""Every closed trade gets a verdict, and a recomputed one says so.

2026-09-18: four trades salvaged out of the corrupt database showed "—" in the
Journal's verdict column. The label is derived from numbers that survived, so it
could always have been recomputed -- but the only thing that recomputed it was a
run-once repair that had already marked itself done before those rows existed.
"""
from __future__ import annotations

import json

import pytest


def _insert_trade(db, **over):
    row = dict(symbol="UNI", strategy="day_climb", mode="paper",
               qty=19.899, entry_px=7.279, exit_px=7.75,
               ts_open=1789600000, ts_close=1789620000, holding_s=20000,
               gross_pnl_usd=9.0829, cost_usd=2.9698, net_pnl_usd=6.1131,
               predicted_edge_bps=None, attribution_json=None)
    row.update(over)
    cols = ",".join(row)
    db.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(row))})",
               tuple(row.values()))
    return db.query_one("SELECT MAX(id) id FROM trades")["id"]


class TestVerdictLabelling:

    def test_a_trade_with_no_attribution_gets_a_verdict(self, writable_db):
        from app.core import db
        from app.feedback import loop
        tid = _insert_trade(db)
        out = loop.attribute_trade(tid)
        assert out["verdict"] == "right, and it paid"

    def test_recovery_provenance_survives_being_relabelled(self, writable_db):
        """The only record that a row was salvaged must not be overwritten."""
        from app.core import db
        from app.feedback import loop
        tid = _insert_trade(db, attribution_json=json.dumps(
            {"provenance": "salvaged_from_corrupt_pages_2026_09_18"}))
        out = loop.attribute_trade(tid)
        assert out["provenance"] == "salvaged_from_corrupt_pages_2026_09_18"
        assert out["verdict"]
        stored = json.loads(db.query_one(
            "SELECT attribution_json a FROM trades WHERE id=?", (tid,))["a"])
        assert stored["provenance"] == "salvaged_from_corrupt_pages_2026_09_18"

    def test_a_recomputed_verdict_says_it_was_recomputed(self, writable_db):
        """An honest gap beats a gap that has been made to look ordinary."""
        from app.core import db
        from app.feedback import loop
        tid = _insert_trade(db, attribution_json=json.dumps(
            {"provenance": "salvaged_from_corrupt_pages_2026_09_18"}))
        out = loop.attribute_trade(tid)
        assert out.get("verdict_recomputed_at"), "recomputed verdict is unmarked"
        assert "recomputed" in out.get("verdict_note", "")

    def test_an_ordinary_trade_is_not_marked_recomputed(self, writable_db):
        from app.core import db
        from app.feedback import loop
        tid = _insert_trade(db)
        out = loop.attribute_trade(tid)
        assert "verdict_recomputed_at" not in out

    def test_a_verdict_written_at_close_is_not_overwritten_as_recomputed(self, writable_db):
        """Re-labelling a row that already had a verdict is not a recovery."""
        from app.core import db
        from app.feedback import loop
        tid = _insert_trade(db, attribution_json=json.dumps(
            {"provenance": "salvaged", "verdict": "right, and it paid"}))
        out = loop.attribute_trade(tid)
        assert "verdict_recomputed_at" not in out


class TestContinuousBackfill:
    """Not a one-shot. A one-shot is what let this happen."""

    def test_it_labels_every_trade_that_has_none(self, writable_db):
        from app.core import db
        from app.feedback import loop
        ids = [_insert_trade(db) for _ in range(3)]
        _insert_trade(db, attribution_json=json.dumps({"verdict": "wrong direction"}))
        out = loop.label_unlabelled_trades()
        assert out["labelled"] == 3, out
        for tid in ids:
            a = json.loads(db.query_one(
                "SELECT attribution_json a FROM trades WHERE id=?", (tid,))["a"])
            assert a["verdict"]

    def test_it_is_idempotent(self, writable_db):
        from app.core import db
        from app.feedback import loop
        _insert_trade(db)
        assert loop.label_unlabelled_trades()["labelled"] == 1
        assert loop.label_unlabelled_trades()["labelled"] == 0

    def test_it_is_not_gated_by_a_run_once_flag(self):
        """The whole point. A flag is why four rows stayed blank."""
        import inspect
        from app.feedback import loop
        src = inspect.getsource(loop.label_unlabelled_trades)
        assert "app_state" not in src, "the backfill is gated by a one-shot flag again"

    def test_boot_calls_it(self):
        """A repair nobody calls is not a repair (blueprint 5.3)."""
        from pathlib import Path
        main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        assert "label_unlabelled_trades()" in main
