"""The database died on 2026-09-18. These are the tests that would have stopped it.

Three separate failures had to line up:

  1. A process on another machine opened the live file read-write while the app
     held it, because SQLite's locking does not cross the bridge mount.
  2. When the file came back unreadable, the app crashed instead of repairing,
     and the supervisor crash-looped for five hours next to a valid backup.
  3. The newest backup was eighteen hours old, because backing up 450 MB of
     refetchable candles is too expensive to do often.

One test class each. If any of these ever goes red, the same day repeats.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.core import dbguard, dbrecover


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_db(path: Path, trades: int = 3) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, pnl REAL)")
    conn.execute("CREATE TABLE bars (id INTEGER PRIMARY KEY, px REAL)")
    conn.executemany("INSERT INTO trades(sym, pnl) VALUES (?,?)",
                     [(f"C{i}", float(i)) for i in range(trades)])
    conn.executemany("INSERT INTO bars(id, px) VALUES (?,?)",
                     [(i, float(i)) for i in range(50)])
    conn.commit()
    conn.close()
    return path


def _destroy(path: Path) -> None:
    """Exactly what we found: right size, no SQLite header anywhere."""
    size = path.stat().st_size
    path.write_bytes(os.urandom(min(size, 8192)) + b"\x00" * max(0, size - 8192))


def _write_claim(db_file: Path, *, host: str, system: str, pid: int,
                 age_s: float = 0.0) -> None:
    import json
    now = time.time() - age_s
    dbguard.sentinel_path(db_file).write_text(json.dumps(
        {"host": host, "system": system, "pid": pid, "role": "test",
         "since": now, "heartbeat": now}))


# ── 1. two writers can never happen again ────────────────────────────────────

class TestDailyBackup:
    """2026-09-18: every daily backup since the 17th failed with "cannot VACUUM
    from within a transaction", and the job recorded status "ok" anyway."""

    def test_vacuum_into_fails_on_a_connection_with_an_open_transaction(self, tmp_path):
        """The bug, pinned. This is why backup() may not use db.get_conn()."""
        src = tmp_path / "t.sqlite"
        c = sqlite3.connect(src)
        c.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, sym TEXT)")
        c.execute("INSERT INTO trades (sym) VALUES ('ARB')")   # implicit txn open
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            c.execute("VACUUM INTO ?", (str(tmp_path / "out.sqlite"),))
        c.close()

    def test_backup_succeeds_while_the_app_is_mid_write(self, writable_db, monkeypatch):
        """The desk is always writing. A backup that only works on an idle
        database is a backup that never runs."""
        from app.core import db as _db, housekeeping
        # The disk-headroom guard is real and correct; it is just sized for a
        # 450 MB production file and this fixture's database is ~0 MB. Lower it
        # so this test exercises VACUUM INTO rather than the headroom refusal,
        # which has its own test.
        monkeypatch.setattr(housekeeping, "BACKUP_MIN_FREE_MB", 1)
        _db.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)")
        _db.get_conn().execute("INSERT INTO probe DEFAULT VALUES")  # uncommitted
        out = housekeeping.backup("test")
        assert out.get("ok"), f"backup failed: {out.get('error')}"
        assert Path(out["file"]).stat().st_size > 0

    def test_ledger_window_shrinks_with_the_disk(self, writable_db, monkeypatch):
        """72 snapshots is 1.4 GB. On a low disk the window halves; on a
        critical one it quarters; it never drops under six."""
        from app.core import db as _db, housekeeping, health
        _db.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)")
        ledger = Path(writable_db).parent / housekeeping.LEDGER_DIRNAME
        ledger.mkdir(exist_ok=True)
        for i in range(80):                                   # 80 old snapshots on disk
            (ledger / f"ledger-20260101-{i:06d}Z.sqlite").write_bytes(b"x")

        def _run(free_gb):
            monkeypatch.setattr(health, "disk", lambda: {"free_gb": free_gb})
            out = housekeeping.ledger_snapshot("test")
            assert out.get("ok")
            return len(list(ledger.glob("ledger-*.sqlite")))

        assert _run(50.0) == housekeeping.LEDGER_KEEP                 # normal: 72
        assert _run(10.8) == housekeeping.LEDGER_KEEP // 2            # low: 36
        assert _run(5.0) == max(6, housekeeping.LEDGER_KEEP // 4)     # critical: 18

    def test_backup_filenames_are_utc_and_say_so(self, writable_db, monkeypatch):
        from app.core import db as _db, housekeeping
        monkeypatch.setattr(housekeeping, "BACKUP_MIN_FREE_MB", 1)
        _db.get_conn()                      # the fixture is lazy; make the file exist
        out = housekeeping.backup("test")
        assert out.get("ok"), out.get("error")
        name = Path(out["file"]).name
        assert name.endswith("Z.sqlite"), f"not marked UTC: {name}"
        stamp = name.rsplit("-", 1)[-1][:-len("Z.sqlite")]
        made = datetime.datetime.strptime(stamp, "%H%M%S").time()
        now = datetime.datetime.now(datetime.timezone.utc).time()
        drift = abs((made.hour * 3600 + made.minute * 60)
                    - (now.hour * 3600 + now.minute * 60))
        assert min(drift, 86400 - drift) < 300, \
            f"filename clock is not UTC: {made} vs {now}"

    def test_a_failed_backup_fails_the_job_instead_of_summarising_it(self, monkeypatch):
        """status "ok" next to the words BACKUP FAILED is how it went unnoticed."""
        from app.core import scheduler, housekeeping
        monkeypatch.setattr(housekeeping, "backup",
                            lambda *a, **k: {"ok": False, "error": "boom"})
        monkeypatch.setattr(housekeeping, "run",
                            lambda *a, **k: {"freed_mb": 0, "after_mb": 1,
                                             "free_gb": 9.9, "vacuumed": False})
        from app.core import dbrecover
        monkeypatch.setattr(dbrecover, "opens_cleanly", lambda *a, **k: (True, "", False))
        with pytest.raises(RuntimeError, match="daily backup failed"):
            scheduler._job_housekeeping()


class TestCommitLatency:
    """webapp_blueprint CHECKLIST 4.20 — WAL alone still fsyncs every commit."""

    def test_connections_are_opened_with_synchronous_normal(self, writable_db):
        from app.core import db as _db
        conn = _db.get_conn()
        mode = conn.execute("PRAGMA synchronous").fetchone()[0]
        # 0 OFF · 1 NORMAL · 2 FULL · 3 EXTRA. FULL is the default and costs an
        # fsync on every commit, on the same loop that has to notice a stop.
        assert mode == 1, f"synchronous={mode}, expected 1 (NORMAL)"

    def test_the_file_is_still_in_wal_mode(self, writable_db):
        from app.core import db as _db
        conn = _db.get_conn()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


class TestZombieLiveness:
    """webapp_blueprint CHECKLIST 1.6 — `kill -0` succeeds on a zombie."""

    def test_a_zombie_does_not_count_as_alive(self, monkeypatch):
        class _R:
            stdout = "Z+\n"
        monkeypatch.setattr(dbguard.subprocess, "run", lambda *a, **k: _R())
        assert dbguard._pid_alive(os.getpid()) is False

    def test_a_running_process_still_counts_as_alive(self, monkeypatch):
        class _R:
            stdout = "S+\n"
        monkeypatch.setattr(dbguard.subprocess, "run", lambda *a, **k: _R())
        assert dbguard._pid_alive(os.getpid()) is True

    def test_an_unreadable_process_table_never_declares_a_pid_dead(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("ps is not available here")
        monkeypatch.setattr(dbguard.subprocess, "run", _boom)
        assert dbguard._pid_alive(os.getpid()) is True

    def test_a_pid_that_does_not_exist_is_still_dead(self):
        assert dbguard._pid_alive(2_000_000_000) is False


class TestOwnershipGuard:

    def test_a_heartbeat_does_not_reset_the_claim_start_time(self, tmp_path):
        """`since` answers "up since when". The beat loop must not clobber it."""
        db = _make_db(tmp_path / "t.sqlite")
        mine = dbguard.claim(db, role="app")
        first = dbguard.read_claim(db)
        assert first is not None and first.since == mine.since
        time.sleep(0.05)
        assert dbguard.beat(db, role="app") is True
        after = dbguard.read_claim(db)
        assert after is not None
        assert after.since == first.since, "the heartbeat reset the claim start time"
        assert after.heartbeat > first.heartbeat, "the heartbeat did not advance"


    def test_a_live_claim_from_another_machine_blocks_us(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        _write_claim(db, host="owner-mac", system="Darwin", pid=999999, age_s=5)
        with pytest.raises(dbguard.DatabaseInUse) as e:
            dbguard.claim(db, role="vm-script")
        # The message has to name the holder and the way out, or nobody learns
        # -- and the way out must be the SAFE one. An earlier version of this
        # message recommended `mode=ro`, which still maps and writes the WAL
        # -shm index from another machine; following that advice is what put a
        # burst of "file is not a database" through a healthy desk.
        msg = str(e.value)
        assert "owner-mac" in msg
        assert "immutable=1" in msg
        assert "?mode=ro', uri=True)" not in msg

    def test_this_is_the_exact_call_that_corrupted_the_database(self, tmp_path):
        """The app holds it on macOS; a script on Linux tries to write. Denied."""
        db = _make_db(tmp_path / "t.sqlite")
        _write_claim(db, host="owner-mac", system="Darwin",
                     pid=4242, age_s=dbguard.BEAT_EVERY_S / 2)
        with pytest.raises(dbguard.DatabaseInUse):
            dbguard.claim(db, role="exit_lab.absorb")

    def test_a_stale_foreign_claim_is_taken_over(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        _write_claim(db, host="gone", system="Darwin", pid=1,
                     age_s=dbguard.STALE_AFTER_S + 30)
        mine = dbguard.claim(db, role="app", confirm=False)
        assert dbguard.read_claim(db).pid == mine.pid

    def test_our_own_dead_process_never_delays_a_restart(self, tmp_path):
        """The supervisor restarts in seconds; it must not wait out a heartbeat."""
        db = _make_db(tmp_path / "t.sqlite")
        import platform
        dead = 2 ** 22          # same host, certainly not running
        _write_claim(db, host=platform.node(), system=platform.system(),
                     pid=dead, age_s=1.0)
        mine = dbguard.claim(db, role="app", confirm=False)
        assert dbguard.read_claim(db).pid == mine.pid != dead

    def test_a_live_process_on_our_own_host_still_blocks(self, tmp_path):
        """Two copies of the app on one machine is the same corruption."""
        db = _make_db(tmp_path / "t.sqlite")
        import platform
        _write_claim(db, host=platform.node(), system=platform.system(),
                     pid=os.getppid(), age_s=1.0)
        with pytest.raises(dbguard.DatabaseInUse):
            dbguard.claim(db, role="second-copy")

    def test_a_recycled_pid_cannot_lock_the_desk_out_of_its_own_database(self, tmp_path):
        """The app dies, the OS hands its pid to something unrelated, the desk
        restarts. If a live pid alone were enough to block, the only way back in
        would be deleting a file by hand at the worst possible moment."""
        db = _make_db(tmp_path / "t.sqlite")
        import platform
        _write_claim(db, host=platform.node(), system=platform.system(),
                     pid=os.getpid(), age_s=dbguard.STALE_AFTER_S + 60)
        mine = dbguard.claim(db, role="app", confirm=False)
        assert dbguard.read_claim(db).since == mine.since

    def test_release_never_removes_somebody_elses_claim(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        _write_claim(db, host="other", system="Darwin", pid=77, age_s=1)
        dbguard.release(db)
        assert dbguard.read_claim(db).pid == 77

    def test_beat_reports_when_we_have_been_displaced(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        dbguard.claim(db, confirm=False)
        _write_claim(db, host="other", system="Darwin", pid=77, age_s=0)
        assert dbguard.beat(db) is False

    def test_an_unreadable_sentinel_does_not_lock_us_out_forever(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        dbguard.sentinel_path(db).write_text("{ this is not json")
        dbguard.claim(db, confirm=False)        # must not raise


# ── 2. a corrupt file is a repair, not a crash loop ──────────────────────────

class TestRecovery:

    def test_a_healthy_database_is_left_completely_alone(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        before = db.read_bytes()
        rep = dbrecover.preflight(db)
        assert rep.healthy and not rep.restored
        assert db.read_bytes() == before

    def test_the_file_we_actually_found_is_detected(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        _destroy(db)
        ok, why, corrupt = dbrecover.opens_cleanly(db)
        assert not ok and corrupt and "not a database" in why

    def test_it_restores_from_the_newest_backup_that_opens(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite", trades=3)
        bk = tmp_path / "backups"
        bk.mkdir()
        _make_db(bk / "t-20260916-055211.sqlite", trades=1)
        _make_db(bk / "t-20260917-055231.sqlite", trades=9)
        _destroy(db)

        rep = dbrecover.preflight(db)
        assert rep.healthy and rep.restored
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 9
        conn.close()

    def test_a_corrupt_backup_is_skipped_for_an_older_good_one(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        bk = tmp_path / "backups"
        bk.mkdir()
        _make_db(bk / "t-20260916-055211.sqlite", trades=4)
        _destroy(_make_db(bk / "t-20260917-055231.sqlite", trades=99))
        _destroy(db)

        rep = dbrecover.preflight(db)
        assert rep.restored and "20260916" in rep.restored_from.name
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 4
        conn.close()

    def test_the_corrupt_file_is_kept_not_deleted(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        bk = tmp_path / "backups"
        bk.mkdir()
        _make_db(bk / "t-20260917-055231.sqlite", trades=2)
        _destroy(db)
        rep = dbrecover.preflight(db)
        assert rep.quarantined and rep.quarantined.exists()

    @pytest.mark.parametrize("exc, is_corruption", [
        (sqlite3.DatabaseError("file is not a database"), True),
        (sqlite3.DatabaseError("database disk image is malformed"), True),
        (sqlite3.OperationalError("database disk image is malformed"), True),
        (sqlite3.OperationalError("disk I/O error"), False),
        (sqlite3.OperationalError("attempt to write a readonly database"), False),
        (sqlite3.OperationalError("unable to open database file"), False),
        (sqlite3.OperationalError("database is locked"), False),
    ])
    def test_only_real_damage_counts_as_corruption(self, exc, is_corruption):
        assert dbrecover._is_corruption(exc) is is_corruption

    def test_an_io_error_is_never_mistaken_for_corruption(self, tmp_path, monkeypatch):
        """A full disk or a dropped mount must not trigger a restore. Quarantining
        a healthy database over a transient problem is how a five-minute outage
        becomes permanent data loss. This goes through the real classifier."""
        db = _make_db(tmp_path / "t.sqlite")
        bk = tmp_path / "backups"
        bk.mkdir()
        _make_db(bk / "t-20260917-055231.sqlite", trades=1)
        before = db.read_bytes()

        real_connect = sqlite3.connect

        def flaky(target, *a, **kw):      # not `uri`: sqlite3 passes uri= itself
            if "t.sqlite" in str(target) and "backups" not in str(target):
                raise sqlite3.OperationalError("disk I/O error")
            return real_connect(target, *a, **kw)

        monkeypatch.setattr(dbrecover.sqlite3, "connect", flaky)
        rep = dbrecover.preflight(db)

        assert not rep.healthy and not rep.restored and not rep.corrupt
        assert rep.quarantined is None
        assert db.read_bytes() == before

    def test_with_no_backup_it_says_so_and_destroys_nothing(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite")
        _destroy(db)
        bad = db.read_bytes()
        rep = dbrecover.preflight(db)
        assert not rep.healthy and not rep.restored
        # An empty database silently replacing a corrupt one hides the loss.
        assert db.read_bytes() == bad

    def test_it_never_reports_health_it_did_not_verify(self, tmp_path):
        """A backup that is itself unreadable must not be reported as a rescue."""
        db = _make_db(tmp_path / "t.sqlite")
        bk = tmp_path / "backups"
        bk.mkdir()
        _destroy(_make_db(bk / "t-20260917-055231.sqlite", trades=5))
        _destroy(db)
        rep = dbrecover.preflight(db)
        assert not rep.healthy and not rep.restored


# ── 3. the eighteen-hour hole ────────────────────────────────────────────────

class TestLedgerMerge:

    def _ledger(self, path: Path, trades: int) -> Path:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, pnl REAL)")
        conn.executemany("INSERT INTO trades(id, sym, pnl) VALUES (?,?,?)",
                         [(i + 1, f"C{i}", float(i)) for i in range(trades)])
        conn.commit()
        conn.close()
        return path

    def test_the_whole_incident_replayed_end_to_end(self, tmp_path):
        """Backup at 17 trades, ledger at 27, file destroyed. All 27 come back."""
        db = _make_db(tmp_path / "t.sqlite", trades=27)
        bk = tmp_path / "backups"
        bk.mkdir()
        _make_db(bk / "t-20260917-055231.sqlite", trades=17)
        led = tmp_path / "ledger"
        led.mkdir()
        self._ledger(led / "ledger-20260918-044500.sqlite", trades=27)
        _destroy(db)

        rep = dbrecover.preflight(db)
        assert rep.healthy and rep.restored

        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 27
        conn.close()
        assert any("recovered" in n for n in rep.notes)

    def test_merging_twice_does_not_duplicate_anything(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite", trades=5)
        led = self._ledger(tmp_path / "l.sqlite", trades=9)
        dbrecover.merge_ledger(db, led)
        dbrecover.merge_ledger(db, led)
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 9
        conn.close()

    def test_the_next_trade_written_after_a_merge_is_not_lost(self, tmp_path):
        """Merged ids sit above the autoincrement counter; if it is not caught
        up, the very next insert collides and the trade silently vanishes."""
        db = _make_db(tmp_path / "t.sqlite", trades=3)
        led = self._ledger(tmp_path / "l.sqlite", trades=30)
        dbrecover.merge_ledger(db, led)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trades(sym, pnl) VALUES ('NEW', 1.0)")
        conn.commit()
        rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        newest = conn.execute("SELECT id FROM trades WHERE sym='NEW'").fetchone()[0]
        conn.close()
        assert rows == 31 and newest > 30

    def test_a_merge_never_hands_out_an_id_twice(self, tmp_path):
        """After deletes, the autoincrement counter sits ABOVE every surviving
        row on purpose. A merge that resets it to the max rowid reissues ids
        that already belonged to something, and every reference to the old row
        silently starts pointing at the new one."""
        db = tmp_path / "t.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, pnl REAL)")
        conn.executemany("INSERT INTO trades(sym, pnl) VALUES (?,?)",
                         [(f"C{i}", float(i)) for i in range(10)])
        conn.execute("DELETE FROM trades WHERE id > 5")     # counter stays at 10
        conn.commit()
        conn.close()

        led = self._ledger(tmp_path / "l.sqlite", trades=5)  # nothing new
        dbrecover.merge_ledger(db, led)

        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO trades(sym, pnl) VALUES ('NEW', 0.0)")
        conn.commit()
        fresh = conn.execute("SELECT id FROM trades WHERE sym='NEW'").fetchone()[0]
        conn.close()
        assert fresh > 10, f"id {fresh} was already used once"

    def test_a_column_added_since_the_snapshot_does_not_shift_values(self, tmp_path):
        db = _make_db(tmp_path / "t.sqlite", trades=1)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE trades ADD COLUMN note TEXT")
        conn.commit()
        conn.close()
        led = self._ledger(tmp_path / "l.sqlite", trades=4)   # no `note` column
        dbrecover.merge_ledger(db, led)
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT sym, pnl, note FROM trades WHERE id=4").fetchone()
        conn.close()
        assert row == ("C3", 3.0, None)


class TestSchedulerIntervalsReachProduction:
    """`INSERT OR IGNORE` never updates a row that already exists, so for weeks
    the code said "collect minute bars hourly", the comment explained exactly
    why six hours was too slow, and the database went on saying six hours. The
    change was real, reviewed, and completely inert."""

    def _seed_with(self, interval_s, code_default):
        from app.core import db, scheduler
        db.init_db()
        scheduler.ensure_schema()
        try:
            db.execute("ALTER TABLE scheduler_jobs ADD COLUMN code_default_s REAL")
        except Exception:
            pass
        db.execute("DELETE FROM scheduler_jobs WHERE name='minute_topup'")
        db.execute("INSERT INTO scheduler_jobs(name, interval_s, enabled, code_default_s) "
                   "VALUES ('minute_topup', ?, 1, ?)", (interval_s, code_default))
        scheduler._seed()
        return db.query_one("SELECT interval_s, code_default_s FROM scheduler_jobs "
                            "WHERE name='minute_topup'")

    def test_a_changed_interval_actually_reaches_the_database(self, writable_db):
        from app.core import scheduler
        want = scheduler.JOBS["minute_topup"]["interval"]
        row = self._seed_with(6 * 3600.0, None)          # legacy row, old value
        assert abs(row["interval_s"] - want) < 1.0

    def test_an_interval_a_human_set_is_left_alone(self, writable_db):
        from app.core import scheduler
        want = scheduler.JOBS["minute_topup"]["interval"]
        # code last asked for `want`; someone deliberately moved it to 3h
        row = self._seed_with(3 * 3600.0, want)
        assert abs(row["interval_s"] - 3 * 3600.0) < 1.0

    def test_a_brand_new_job_records_the_default_it_came_from(self, writable_db):
        from app.core import db, scheduler
        db.init_db()
        scheduler._seed()
        row = db.query_one("SELECT interval_s, code_default_s FROM scheduler_jobs "
                           "WHERE name='ledger'")
        assert row is not None
        assert abs(row["interval_s"] - row["code_default_s"]) < 1.0
