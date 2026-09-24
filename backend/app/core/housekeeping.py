"""Retention and compaction — so the app does not become the thing filling the disk.

What is kept, and why:

    1-minute bars    RETENTION_MINUTE_DAYS. These are the fastest-growing table by
                     far: 50 coins x 1440 bars a day is 72,000 rows daily, roughly
                     9 MB a week. They only matter to the fast strategies, over
                     short windows.
    15-minute bars   kept longer; the middle ground for intraday research.
    hourly / daily   kept forever. This is the backfilled history everything is
                     validated on, it does not grow quickly, and re-downloading
                     four years is not free.
    quotes           RETENTION_QUOTE_DAYS. Only used to measure spreads and mark
                     positions; there is no research value in last month's ticks.
    events           RETENTION_EVENT_DAYS, except errors, which are kept.

VACUUM is run only when the free-page count is high enough to be worth it, because
it rewrites the whole file and briefly needs as much space again.
"""
from __future__ import annotations

import os as _os
import shutil
import time as _time
import time
from pathlib import Path

from app.config import get_settings
from app.core import db, health

# Measured 2026-09-10 from three consecutive daily VACUUM backups:
#   09-08  470.7 MB -> 09-09  480.9 MB -> 09-10  491.0 MB
# = +9.7 MB of compacted database a day, at ~77,000 new bar rows a day, of
# which 68,000 are 1-minute bars. The minute table is 88% of the growth and the
# only thing that reads it is price_basis, which falls back to it when a quote
# is missing -- and quotes are kept 14 days. Keeping minute bars three times
# longer than the quotes they stand in for buys nothing: at 45 days it settles
# near 640 MB, at 14 days near 200 MB.
RETENTION_MINUTE_DAYS = 14
RETENTION_15MIN_DAYS = 400
RETENTION_QUOTE_DAYS = 14
RETENTION_EVENT_DAYS = 60
VACUUM_FREE_PAGE_RATIO = 0.20

# When the disk is genuinely tight, keep less. This app should never be the
# reason a write fails halfway, because that is how the database got corrupted
# twice. Below the first threshold retention halves; below the second it is
# quartered and the vacuum runs at a much lower bar.
LOW_DISK_GB = 15.0
CRITICAL_DISK_GB = 8.0


def _retention_scale(free_gb: float | None) -> tuple[float, str]:
    """How much of the normal retention to keep, given the free space."""
    if free_gb is None:
        return 1.0, "normal"
    if free_gb < CRITICAL_DISK_GB:
        return 0.25, f"CRITICAL — only {free_gb:.1f} GB free, keeping a quarter of the usual window"
    if free_gb < LOW_DISK_GB:
        return 0.5, f"low disk — {free_gb:.1f} GB free, keeping half the usual window"
    return 1.0, "normal"


def run(dry_run: bool = False) -> dict:
    now = time.time()
    before = health.disk()
    actions = []
    scale, scale_note = _retention_scale(before.get("free_gb"))
    # A dry run WRITES NOTHING -- not even the warning. 2026-09-20: the Mac
    # dropped under the low-disk line, this line fired inside the test suite's
    # read-only dry run, and two health tests went red for "attempt to write a
    # readonly database" while the actual finding (disk is low) went unread.
    if scale < 1.0 and not dry_run:
        db.log_event("WARNING", "housekeeping", f"retention tightened: {scale_note}")

    plans = [
        ("1-minute bars", "DELETE FROM bars WHERE granularity=60 AND ts < ?",
         now - RETENTION_MINUTE_DAYS * scale * 86400,
         f"only the last {RETENTION_MINUTE_DAYS} days matter to the fast strategies"),
        ("15-minute bars", "DELETE FROM bars WHERE granularity=900 AND ts < ?",
         now - RETENTION_15MIN_DAYS * scale * 86400,
         f"kept {RETENTION_15MIN_DAYS} days for intraday research"),
        ("quotes", "DELETE FROM quotes WHERE ts < ?",
         now - RETENTION_QUOTE_DAYS * scale * 86400,
         "ticks older than a fortnight have no research value"),
        ("events", "DELETE FROM events WHERE ts < ? AND level NOT IN ('ERROR','CRITICAL')",
         now - RETENTION_EVENT_DAYS * scale * 86400,
         "errors are kept regardless of age"),
    ]

    for label, sql, cutoff, why in plans:
        count_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) c FROM", 1)
        try:
            n = db.query_one(count_sql, (cutoff,))["c"]
        except Exception as exc:
            actions.append({"table": label, "error": str(exc)})
            continue
        if n and not dry_run:
            db.execute(sql, (cutoff,))
        actions.append({"table": label, "rows": n, "why": why,
                        "cutoff": time.strftime("%Y-%m-%d", time.localtime(cutoff)),
                        "deleted": bool(n and not dry_run)})

    checkpointed = False
    vacuumed = False
    if not dry_run:
        try:
            db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            checkpointed = True
        except Exception:
            pass
        try:
            free = db.get_conn().execute("PRAGMA freelist_count").fetchone()[0]
            total = db.get_conn().execute("PRAGMA page_count").fetchone()[0]
            # A tight disk lowers the bar for reclaiming free pages: the live
            # file carried 105 MB of them at 17.6%, just under the 20% gate, so
            # it never reclaimed while the disk filled underneath it.
            ratio_gate = VACUUM_FREE_PAGE_RATIO if scale >= 1.0 else 0.08
            if total and free / total > ratio_gate:
                # VACUUM briefly needs as much free disk as the file itself.
                if (before.get("free_gb") or 0) * 1000 > before.get("database_mb", 0) * 2:
                    db.get_conn().execute("VACUUM")
                    vacuumed = True
        except Exception:
            pass

    after = health.disk()
    return {
        "dry_run": dry_run,
        "actions": actions,
        "wal_checkpointed": checkpointed,
        "vacuumed": vacuumed,
        "before_mb": before.get("project_mb"),
        "after_mb": after.get("project_mb"),
        "freed_mb": (before.get("project_mb", 0) - after.get("project_mb", 0)),
        "free_gb": after.get("free_gb"),
        "retention": {
            "scale_applied": scale,
            "scale_note": scale_note,
            "minute_bars_days": RETENTION_MINUTE_DAYS * scale,
            "fifteen_min_days": RETENTION_15MIN_DAYS,
            "quotes_days": RETENTION_QUOTE_DAYS,
            "events_days": RETENTION_EVENT_DAYS,
            "hourly_and_daily": "kept forever — this is the history everything is validated on",
        },
    }

# ── ledger snapshots ─────────────────────────────────────────────────────────
# The full backup runs daily and is ~450 MB, and 99% of that is bars and quotes
# -- which Coinbase will hand back any time we ask. Everything that CANNOT be
# refetched (trades, orders, positions, signals, the equity curve, the models,
# the exit study) is about 75,000 rows: a few megabytes. Snapshotting only that
# is cheap enough to do every few minutes, and that is the difference between
# losing eighteen hours of trading and losing ten minutes of it.
LEDGER_SKIP = {"bars", "quotes"}
LEDGER_KEEP = 72
LEDGER_DIRNAME = "ledger"


def _ledger_dir(db_file) -> Path:
    d = Path(db_file).parent / LEDGER_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def ledger_snapshot(reason: str = "scheduled") -> dict:
    """Copy every irreplaceable table out to its own small database file.

    Reads through a separate read-only connection inside one transaction, so it
    is a consistent point-in-time view and it cannot interfere with trading.
    """
    import sqlite3 as _sq
    s = get_settings()
    db_file = Path(s.db_file)
    # UTC + Z, for the same reason as backup() above.
    dest = _ledger_dir(db_file) / f"ledger-{_time.strftime('%Y%m%d-%H%M%S', _time.gmtime())}Z.sqlite"

    src_conn = _sq.connect(f"file:{db_file}?mode=ro", uri=True, timeout=20)
    try:
        src_conn.execute("BEGIN")          # one consistent read snapshot
        meta = [(r[0], r[1]) for r in src_conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")]
        captured = {}
        for name, ddl in meta:
            if name in LEDGER_SKIP:
                continue
            cur = src_conn.execute(f'SELECT * FROM "{name}"')
            cols = [d[0] for d in cur.description]
            captured[name] = (ddl, cols, cur.fetchall())
    finally:
        try:
            src_conn.rollback()
        except Exception:
            pass
        src_conn.close()

    tmp = dest.with_suffix(".partial")
    out = _sq.connect(tmp)
    try:
        rows_total = 0
        for name, (ddl, cols, rows) in captured.items():
            out.execute(ddl)
            if rows:
                marks = ",".join("?" * len(cols))
                collist = ",".join(f'"{c}"' for c in cols)
                out.executemany(f'INSERT INTO "{name}"({collist}) VALUES ({marks})', rows)
                rows_total += len(rows)
        out.commit()
    finally:
        out.close()
    _os.replace(tmp, dest)                 # never leave a half-written snapshot

    # The same disk rule as the tables: a tight disk keeps a shorter window.
    # 72 snapshots is twelve hours at the job's cadence, 1.4 GB at today's
    # 21 MB each -- on a Mac that was down to 10.8 GB on 2026-09-21. Half the
    # window when the disk is low, a quarter when it is critical, never fewer
    # than six (an hour and a half of point-in-time ledgers to restore from).
    try:
        scale, _ = _retention_scale(health.disk().get("free_gb"))
    except Exception:
        scale = 1.0
    keep = max(6, int(LEDGER_KEEP * scale))
    kept = sorted(_ledger_dir(db_file).glob("ledger-*.sqlite"), reverse=True)
    for old in kept[keep:]:
        try:
            old.unlink()
        except OSError:
            pass

    size_mb = dest.stat().st_size / 1e6
    db.log_event("INFO", "housekeeping",
                 f"ledger snapshot {dest.name} ({size_mb:.1f} MB, "
                 f"{rows_total:,} rows across {len(captured)} tables, {reason})")
    return {"ok": True, "path": str(dest), "megabytes": size_mb,
            "rows": rows_total, "tables": len(captured)}


def newest_ledger(db_file) -> Path | None:
    d = Path(db_file).parent / LEDGER_DIRNAME
    if not d.is_dir():
        return None
    for cand in sorted(d.glob("ledger-*.sqlite"), reverse=True):
        try:
            if cand.stat().st_size > 0:
                return cand
        except OSError:
            continue
    return None


# ── backups ──────────────────────────────────────────────────────────────────
# Added after the database was corrupted on 2026-09-06 and only survived because
# every byte happened to be recoverable. It will not always be.
#
# The corruption had a specific cause worth recording: the file was written by a
# SECOND process reaching it over a network/FUSE mount while the backend held it
# open in WAL mode. SQLite's locking does not work across that kind of mount, and
# the result was a file truncated four pages short of what its own header claimed.
# Nothing warned; the app simply refused to start with "database disk image is
# malformed".
#
# VACUUM INTO is the right tool here: it produces a compacted, internally
# consistent copy while the database is live, without stopping the app and
# without the half-written-file risk of copying bytes underneath a running writer.
BACKUP_KEEP = 7
# Seven was chosen when this database was small. It is 578 MB now and grows
# every day, so "keep 7" quietly means 3.4 GB of backups -- on a disk that was
# down to 4.4 GB free, 99% full, with the app still writing to it. A backup that
# crowds the disk is worse than no backup: the half-written file it causes is
# precisely how this database got corrupted, twice. So the count is a ceiling,
# not a target, and total size and free space both get a say.
BACKUP_MAX_TOTAL_MB = 1500.0
BACKUP_MIN_FREE_MB = 3000.0


def _prune_backups(out_dir, stem: str, keep: int = BACKUP_KEEP,
                   budget_mb: float = BACKUP_MAX_TOTAL_MB):
    """Keep the newest `keep` backups, and only as many as fit the size budget."""
    kept, removed, total = [], [], 0.0
    for f in sorted(out_dir.glob(f"{stem}-*.sqlite"), reverse=True):
        try:
            mb = f.stat().st_size / 1e6
        except OSError:
            continue
        if len(kept) < keep and (total + mb) <= budget_mb:
            kept.append(f)
            total += mb
        else:
            removed.append(f)
    for f in removed:
        try:
            f.unlink()
        except OSError:
            pass
    return kept, removed, total


def backup(reason: str = "scheduled") -> dict:
    """Take a consistent snapshot. Safe to run while the app is trading."""
    import time as _t
    s = get_settings()
    db_file = s.db_file
    out_dir = db_file.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    # UTC, and say so with the Z. Every timestamp INSIDE these files is epoch
    # seconds rendered as UTC; naming the file in local time meant a snapshot
    # labelled 08:44 held data from 13:44, which is exactly the confusion you do
    # not want while reconstructing an incident. Old local-time names are left
    # alone: they still sort correctly, and renaming history is worse.
    stamp = _t.strftime("%Y%m%d-%H%M%S", _t.gmtime()) + "Z"
    dest = out_dir / f"{db_file.stem}-{stamp}.sqlite"
    # Make room BEFORE writing, and refuse rather than fill the volume.
    try:
        need_mb = db_file.stat().st_size / 1e6
    except OSError:
        need_mb = 0.0
    free_mb = shutil.disk_usage(out_dir).free / 1e6
    if free_mb < need_mb + BACKUP_MIN_FREE_MB:
        _prune_backups(out_dir, db_file.stem, keep=2)
        free_mb = shutil.disk_usage(out_dir).free / 1e6
    if free_mb < need_mb + BACKUP_MIN_FREE_MB:
        msg = (f"refusing to back up: {free_mb:.0f} MB free, the database is "
               f"{need_mb:.0f} MB, and {BACKUP_MIN_FREE_MB:.0f} MB of headroom is "
               f"required. Free disk space first — a backup that fills the volume "
               f"is how the live database gets corrupted.")
        db.log_event("ERROR", "housekeeping", msg)
        return {"ok": False, "error": msg}

    t0 = _t.time()
    try:
        # NOT db.get_conn(). That connection is the app's, and Python's sqlite3
        # opens an implicit transaction on it as soon as anything writes -- so
        # VACUUM INTO raised "cannot VACUUM from within a transaction" on a
        # trading desk that is, by definition, always writing. Every daily
        # backup after 2026-09-17 failed this way, silently, for a day.
        #
        # A dedicated connection in autocommit (isolation_level=None) has no
        # transaction to be inside. mode=ro is enough: VACUUM INTO only writes
        # the DESTINATION, and this way the backup cannot touch the app's
        # transaction state at all.
        import sqlite3 as _sq3
        src = _sq3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=30,
                           isolation_level=None)
        try:
            src.execute("VACUUM INTO ?", (str(dest),))
        finally:
            src.close()
    except Exception as exc:
        db.log_event("ERROR", "housekeeping", f"backup FAILED: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    kept, removed, total_mb = _prune_backups(out_dir, db_file.stem)
    size_mb = dest.stat().st_size / 1e6
    db.log_event("INFO", "housekeeping",
                 f"backup {dest.name} ({size_mb:.0f} MB, {reason}); "
                 f"{len(kept)} kept using {total_mb:.0f} MB, {len(removed)} pruned")
    return {"ok": True, "file": str(dest), "megabytes": size_mb,
            "seconds": _t.time() - t0, "kept": len(kept), "pruned": len(removed),
            "note": ("VACUUM INTO makes a consistent, compacted copy while the app "
                     "keeps running. Restoring is a file move — no import step.")}


def list_backups() -> list[dict]:
    s = get_settings()
    out_dir = s.db_file.parent / "backups"
    if not out_dir.exists():
        return []
    rows = []
    for p in sorted(out_dir.glob(f"{s.db_file.stem}-*.sqlite"), reverse=True):
        st = p.stat()
        rows.append({"file": p.name, "megabytes": st.st_size / 1e6, "mtime": st.st_mtime})
    return rows
