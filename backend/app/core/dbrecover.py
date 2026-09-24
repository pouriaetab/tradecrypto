"""Boot-time integrity check, and self-repair from the newest good backup.

On 2026-09-18 the database was destroyed and the app did the worst possible
thing with it: it crashed on startup, the supervisor restarted it, it crashed
again, and it repeated that for five hours without ever saying the one useful
sentence -- "the file is not a database, here is a backup that is". A verified
440 MB backup was sitting in data/backups the entire time.

So the rule is: a corrupt database is a recoverable condition, not a crash.
Check it before the schema runs, and if it is unreadable, quarantine it and
restore the newest backup that actually opens. Never restore silently -- the
gap between the backup and now is real data that is gone, and it has to be
said out loud, in the log and on the status page.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"SQLite format 3\x00"

# A database that will not open is not automatically a broken one. A full disk,
# a mount that has gone away, a permissions change, a WAL companion we cannot
# map -- all of those raise from sqlite3 too, and "restoring" over them would
# quarantine a healthy file and discard everything since the last backup. Only
# these mean the bytes themselves are gone.
CORRUPTION_SIGNS = ("not a database", "malformed", "is corrupt", "corrupted",
                    "encrypted", "unsupported file format")

# quick_check walks every page, which is tens of seconds on a 450 MB file. Boot
# does the cheap checks (header, schema readable); the deep one runs in daily
# housekeeping where nobody is waiting on it.
DEEP_CHECK_MAX_ERRORS = 10


@dataclass
class Report:
    healthy: bool
    corrupt: bool = False
    restored: bool = False
    reason: str = ""
    quarantined: Path | None = None
    restored_from: Path | None = None
    lost_since: str | None = None
    notes: list[str] = field(default_factory=list)

    def headline(self) -> str:
        if self.healthy and not self.restored:
            return "database ok"
        if self.restored:
            gap = f", data after {self.lost_since} is gone" if self.lost_since else ""
            return (f"database was unreadable ({self.reason}) and has been restored "
                    f"from {self.restored_from.name if self.restored_from else '?'}{gap}")
        if not self.corrupt:
            return f"database could not be opened ({self.reason}) — not corruption"
        return f"database is unreadable ({self.reason}) and no backup could replace it"


def _is_corruption(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(m in msg for m in CORRUPTION_SIGNS):
        return True
    # sqlite3 raises the bare DatabaseError for structural damage, and one of
    # its subclasses (OperationalError and friends) for everything else.
    return type(exc) is sqlite3.DatabaseError


def _looks_like_sqlite(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            return fh.read(16) == MAGIC
    except OSError:
        return False


def opens_cleanly(p: Path, deep: bool = False, *,
                  static: bool = False) -> tuple[bool, str, bool]:
    """(readable, why not, is it corruption).

    The third value is the one that matters: only corruption justifies throwing
    the file away and restoring over it.

    `static` means nothing else can be writing this file -- a backup, or a
    quarantined copy. Those are opened immutable, which skips WAL entirely; a
    freshly restored backup has no -shm companion and creating one is a write,
    so a plain read-only connection to it fails with "attempt to write a
    readonly database" and says nothing at all about its health.
    """
    if not p.exists() or p.stat().st_size == 0:
        return True, "", False                # a new database is not a broken one
    if not _looks_like_sqlite(p):
        return False, "file is not a database (no SQLite header)", True

    uri = f"file:{p}?mode=ro&immutable=1" if static else f"file:{p}?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        return False, f"{type(exc).__name__}: {exc}", _is_corruption(exc)
    try:
        conn.execute("PRAGMA schema_version").fetchone()
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if deep:
            verdict = [r[0] for r in conn.execute(
                f"PRAGMA quick_check({DEEP_CHECK_MAX_ERRORS})").fetchall()]
            if verdict != ["ok"]:
                return False, "; ".join(verdict)[:400], True
    except sqlite3.Error as exc:
        return False, f"{type(exc).__name__}: {exc}", _is_corruption(exc)
    finally:
        conn.close()
    return True, "", False


def _newest_data_ts(p: Path) -> str | None:
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    try:
        for sql in ("SELECT MAX(ts) FROM equity_curve",
                    "SELECT MAX(ts) FROM bars"):
            try:
                v = conn.execute(sql).fetchone()[0]
            except sqlite3.Error:
                continue
            if v:
                try:
                    from datetime import datetime
                    return datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M")
                except (TypeError, ValueError, OSError):
                    return str(v)
    finally:
        conn.close()
    return None


def usable_backups(db_file: Path) -> list[Path]:
    """Backups that actually open, newest first. Verified, not assumed."""
    out_dir = db_file.parent / "backups"
    if not out_dir.is_dir():
        return []
    good = []
    for cand in sorted(out_dir.glob(f"{db_file.stem}-*.sqlite"), reverse=True):
        ok, _, _c = opens_cleanly(cand, deep=False, static=True)
        if ok and cand.stat().st_size > 0:
            good.append(cand)
    return good


def _quarantine(db_file: Path) -> Path:
    q = db_file.parent / "quarantine"
    q.mkdir(parents=True, exist_ok=True)
    dest = q / f"{db_file.stem}-corrupt-{time.strftime('%Y%m%d-%H%M%S')}.sqlite"
    shutil.move(str(db_file), str(dest))
    for side in ("-wal", "-shm"):
        s = Path(str(db_file) + side)
        if s.exists():
            try:
                shutil.move(str(s), str(dest) + side)
            except OSError:
                pass
    return dest


def preflight(db_file: Path, *, allow_restore: bool = True) -> Report:
    """Run before the schema. Repairs if it can; always explains itself."""
    db_file = Path(db_file)
    ok, why, corrupt = opens_cleanly(db_file, deep=False)
    if ok:
        return Report(healthy=True)

    rep = Report(healthy=False, reason=why, corrupt=corrupt)
    if not corrupt:
        # Readable bytes, unreadable situation. Report what SQLite said and stop.
        # Quarantining a healthy database over a full disk or a dropped mount
        # turns a five-minute problem into permanent data loss.
        rep.notes.append("not corruption — the file was left untouched; check "
                         "disk space, permissions, and that the volume is mounted")
        return rep
    if not allow_restore:
        return rep

    candidates = usable_backups(db_file)
    if not candidates:
        rep.notes.append("no usable backup found in data/backups")
        return rep

    newest = candidates[0]
    rep.lost_since = _newest_data_ts(newest)
    rep.quarantined = _quarantine(db_file)
    shutil.copy2(newest, db_file)
    for side in ("-wal", "-shm"):
        s = Path(str(db_file) + side)
        if s.exists():
            try:
                s.unlink()
            except OSError:
                pass

    ok2, why2, _ = opens_cleanly(db_file, deep=False)
    if not ok2:
        rep.reason = f"{why}; restore also unreadable: {why2}"
        return rep

    rep.healthy = True
    rep.restored = True
    rep.restored_from = newest

    # The full backup is up to a day old; the ledger snapshot is minutes old and
    # holds every row that cannot be refetched. Fold it in now, before anything
    # else opens the database and starts issuing row ids of its own.
    led = newest_ledger(db_file)
    if led is not None:
        try:
            m = merge_ledger(db_file, led)
            if m["rows"]:
                rep.notes.append(
                    f"recovered {m['rows']:,} rows from {Path(m['from']).name}: "
                    + ", ".join(f"{k} +{v}" for k, v in sorted(m["added"].items())))
                rep.lost_since = _newest_data_ts(db_file) or rep.lost_since
        except sqlite3.Error as exc:
            rep.notes.append(f"ledger merge failed ({type(exc).__name__}: {exc})")

    rep.notes.append(f"corrupt file kept at {rep.quarantined}")
    rep.notes.append("bars and quotes refill themselves from Coinbase on catch-up; "
                     "trades and signals in the gap are gone for good")
    return rep

LEDGER_DIRNAME = "ledger"


def newest_ledger(db_file: Path) -> Path | None:
    """The most recent small-table snapshot, if there is one."""
    d = Path(db_file).parent / LEDGER_DIRNAME
    if not d.is_dir():
        return None
    for cand in sorted(d.glob("ledger-*.sqlite"), reverse=True):
        try:
            if cand.stat().st_size > 0 and opens_cleanly(cand, static=True)[0]:
                return cand
        except OSError:
            continue
    return None


def merge_ledger(db_file: Path, ledger: Path) -> dict:
    """Fold a ledger snapshot into the database, keeping rows already present.

    ORDER MATTERS. Run this the instant the full backup has been restored and
    before the app writes anything, which is what preflight does. The ledger's
    row ids are only meaningful against that backup's lineage; once the engine
    has started issuing new ids of its own, a merge would quietly drop rows
    whose ids had been handed out again.
    """
    db_file, ledger = Path(db_file), Path(ledger)
    conn = sqlite3.connect(db_file, timeout=30)
    added: dict[str, int] = {}
    try:
        conn.execute("ATTACH DATABASE ? AS led", (str(ledger),))
        names = [r[0] for r in conn.execute(
            "SELECT name FROM led.sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        here = {r[0] for r in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'")}
        for t in names:
            if t not in here:
                continue
            # Column *names*, intersected -- never SELECT *, because a schema
            # change between the snapshot and the backup would silently shift
            # values into the wrong columns.
            mine = [r[1] for r in conn.execute(f'PRAGMA main.table_info("{t}")')]
            theirs = [r[1] for r in conn.execute(f'PRAGMA led.table_info("{t}")')]
            cols = [c for c in mine if c in set(theirs)]
            if not cols:
                continue
            collist = ",".join(f'"{c}"' for c in cols)
            before = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
            conn.execute(f'INSERT OR IGNORE INTO main."{t}"({collist}) '
                         f'SELECT {collist} FROM led."{t}"')
            gained = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0] - before
            if gained:
                added[t] = gained
        # Merged rows can carry ids above the stored autoincrement counter; if
        # that counter is not caught up, the next insert collides and is lost.
        for (t,) in conn.execute("SELECT name FROM main.sqlite_sequence").fetchall():
            try:
                # MAX(seq, max rowid) -- never just the max rowid. AUTOINCREMENT
                # promises an id is never reused, and after rows are deleted the
                # stored counter is deliberately HIGHER than any surviving row.
                # Overwriting it with the max rowid hands those ids out a second
                # time, which silently re-points every foreign key that still
                # refers to the deleted row.
                conn.execute(
                    "UPDATE main.sqlite_sequence SET seq = MAX(seq, "
                    f'(SELECT COALESCE(MAX(rowid), 0) FROM main."{t}")) WHERE name = ?', (t,))
            except sqlite3.Error:
                pass
        conn.commit()
    finally:
        try:
            conn.execute("DETACH DATABASE led")
        except sqlite3.Error:
            pass
        conn.close()
    return {"from": str(ledger), "added": added,
            "rows": sum(added.values())}
