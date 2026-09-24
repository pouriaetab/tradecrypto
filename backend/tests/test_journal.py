"""The copy that survives when the database does not.

2026-09-18: the database was destroyed and a day of trades went with it. There
was nowhere else to look — orders, fills and trades were written to database
tables and nothing else. The operator, correctly: "I don't know if they are real
or not anymore."

A ledger with one copy cannot be checked against anything. These tests hold the
second copy to the only standard that makes it worth having: it survives, it
chains, and it can never take down a trade.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import journal


@pytest.fixture
def jdir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "_dir", lambda: tmp_path)
    monkeypatch.setattr(journal, "_last_hash", None, raising=False)
    return tmp_path


def _lines(d: Path):
    f = sorted(d.glob("*.jsonl"))
    assert f, "nothing was journalled"
    return [json.loads(l) for l in f[0].read_text().splitlines() if l.strip()]


class TestItSurvivesAndChains:

    def test_a_trade_is_written_as_one_self_contained_line(self, jdir):
        assert journal.append("trade_closed", {"symbol": "ARB", "net_usd": 9.03})
        (rec,) = _lines(jdir)
        assert rec["kind"] == "trade_closed"
        assert rec["data"]["symbol"] == "ARB" and rec["data"]["net_usd"] == 9.03
        assert rec["prev"] == journal.GENESIS

    def test_each_line_carries_the_hash_of_the_one_before(self, jdir):
        for i in range(5):
            journal.append("order", {"n": i})
        recs = _lines(jdir)
        assert len(recs) == 5
        f = sorted(jdir.glob("*.jsonl"))[0]
        assert journal.verify(f)["ok"] is True
        assert journal.verify(f)["lines"] == 5

    def test_editing_a_number_breaks_the_chain(self, jdir):
        """The whole point: a silently altered ledger must stop verifying."""
        for i in range(4):
            journal.append("trade_closed", {"net_usd": float(i)})
        f = sorted(jdir.glob("*.jsonl"))[0]
        lines = f.read_text().splitlines()
        lines[1] = lines[1].replace('"net_usd":1.0', '"net_usd":999.0')
        f.write_text("\n".join(lines) + "\n")
        out = journal.verify(f)
        # Detection lands on the line AFTER the edit: line 2's own `prev` still
        # matches line 1, but line 3's `prev` no longer matches the altered line 2.
        assert out["ok"] is False and out["broke_at"] == 3

    def test_deleting_a_line_breaks_the_chain(self, jdir):
        for i in range(4):
            journal.append("order", {"n": i})
        f = sorted(jdir.glob("*.jsonl"))[0]
        lines = f.read_text().splitlines()
        del lines[2]
        f.write_text("\n".join(lines) + "\n")
        assert journal.verify(f)["ok"] is False

    def test_truncating_the_end_still_verifies_up_to_the_cut(self, jdir):
        """A crash mid-write loses the tail. That must not read as tampering of
        everything before it — otherwise the check is useless in the one case it
        exists for."""
        for i in range(6):
            journal.append("order", {"n": i})
        f = sorted(jdir.glob("*.jsonl"))[0]
        lines = f.read_text().splitlines()[:3]
        f.write_text("\n".join(lines) + "\n")
        out = journal.verify(f)
        assert out["ok"] is True and out["lines"] == 3

    def test_a_restart_chains_onto_the_existing_file(self, jdir):
        journal.append("order", {"n": 1})
        journal._last_hash = None                 # as a fresh process would start
        journal.append("order", {"n": 2})
        assert journal.verify(sorted(jdir.glob("*.jsonl"))[0])["ok"] is True


class TestItIsNeverLoadBearing:

    def test_a_broken_journal_never_raises(self, tmp_path, monkeypatch):
        """A safety net that can drop the acrobat is not a safety net."""
        def explode():
            raise OSError("read-only file system")
        monkeypatch.setattr(journal, "_dir", explode)
        monkeypatch.setattr(journal, "_last_hash", None, raising=False)
        assert journal.append("trade_closed", {"symbol": "ARB"}) is False

    def test_unserialisable_payloads_do_not_lose_the_event(self, jdir):
        class Odd:
            def __repr__(self): return "<odd>"
        assert journal.append("trade_closed", {"weird": Odd(), "net_usd": 1.0})
        (rec,) = _lines(jdir)
        assert rec["data"]["net_usd"] == 1.0


class TestAPromiseLeavesARecord:
    """2026-09-17: the operator was told a live ARB position "stops being able to
    lose" once the breakeven ratchet engaged. It was true when it was said. The
    position row then died with the database, so the claim survives only as a
    sentence in a chat log and cannot be checked against anything.

    A forward-looking statement about real money has to leave a record at the
    instant it becomes true, in the one place the database cannot take with it.
    """

    def test_the_breakeven_lock_is_journalled_when_it_engages(self, jdir):
        journal.append("stop_locked_breakeven", {
            "symbol": "ARB", "strategy": "pump_ride", "entry_px": 0.224474,
            "peak_px": 0.22796, "stop_px": 0.22663,
            "covers_both_spreads_at": 0.22663, "qty": 5043.4231})
        (rec,) = _lines(jdir)
        assert rec["kind"] == "stop_locked_breakeven"
        d = rec["data"]
        # The three numbers that make the claim checkable afterwards.
        assert d["symbol"] == "ARB"
        assert d["stop_px"] >= d["covers_both_spreads_at"]
        assert d["stop_px"] > d["entry_px"], "a breakeven lock must sit above the fill"

    def test_the_engine_actually_calls_it(self):
        """The record is worthless if only the test writes it — the same mistake
        as a fix that never reaches production."""
        src = Path(__file__).resolve().parents[1] / "app" / "execution" / "engine.py"
        text = src.read_text()
        assert "stop_locked_breakeven" in text, "the engine does not journal the lock"
        assert "journal.append(" in text


class TestTheChainAcrossMidnight:
    """2026-09-19: the guard reported "chain broken at line 1" every midnight.

    `append()` cached the last hash in a module global with no memory of which
    FILE it came from, so the first line of a new day chained onto the last line
    of the old one while `verify()` started every file at GENESIS. Nothing had
    been tampered with; the tamper alarm had. A guard that cries wolf on a
    schedule is worse than no guard.
    """

    def test_a_new_day_starts_a_new_chain(self, tmp_path, monkeypatch):
        from app.core import journal
        monkeypatch.setattr(journal, "_dir", lambda: tmp_path)
        monkeypatch.setattr(journal, "_last", ("/some/other/file.jsonl", "f" * 64))
        monkeypatch.setattr(journal, "_path_for", lambda ts: tmp_path / "2026-01-02.jsonl")
        assert journal.append("order", {"x": 1}) is True
        line = json.loads((tmp_path / "2026-01-02.jsonl").read_text().splitlines()[0])
        assert line["prev"] == journal.GENESIS, \
            "a new day's first line chained onto a different file"

    def test_a_carried_chain_still_verifies_and_says_so(self, tmp_path, monkeypatch):
        """The files written before the fix are a real chain, just a longer one.
        This is an append-only log: teach the verifier, never rewrite the file."""
        from app.core import journal
        monkeypatch.setattr(journal, "_dir", lambda: tmp_path)
        a, b = tmp_path / "2026-01-01.jsonl", tmp_path / "2026-01-02.jsonl"
        monkeypatch.setattr(journal, "_last", None)
        monkeypatch.setattr(journal, "_path_for", lambda ts: a)
        journal.append("order", {"i": 1})
        journal.append("order", {"i": 2})
        tail = journal._tail_hash(a)
        # day two, written the old way: carrying day one's tail
        monkeypatch.setattr(journal, "_path_for", lambda ts: b)
        monkeypatch.setattr(journal, "_last", (str(b), tail))
        journal.append("order", {"i": 3})
        r = journal.verify(b)
        assert r["ok"] is True
        assert r["chained_from_previous_day"] is True

    def test_line_one_still_cannot_be_anything_it_likes(self, tmp_path, monkeypatch):
        """Accepting the previous file's tail must not mean accepting a value."""
        from app.core import journal
        monkeypatch.setattr(journal, "_dir", lambda: tmp_path)
        f = tmp_path / "2026-01-03.jsonl"
        monkeypatch.setattr(journal, "_path_for", lambda ts: f)
        monkeypatch.setattr(journal, "_last", None)
        journal.append("order", {"i": 1})
        lines = f.read_text().splitlines()
        rec = json.loads(lines[0]); rec["prev"] = "a" * 64
        f.write_text(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")
        r = journal.verify(f)
        assert r["ok"] is False and r["broke_at"] == 1
