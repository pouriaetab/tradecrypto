"""An append-only, tamper-evident record of everything the desk owns or owes.

WHY THIS EXISTS

On 2026-09-18 the database was destroyed and a day of trading went with it. When
we went looking for the records elsewhere, there was nowhere else: orders, fills
and trades were written to database tables and nothing else. The text log had the
strategy banner and nothing about a single trade. The operator's reaction was the
correct one -- "I don't know if they are real or not anymore" -- because a ledger
with exactly one copy cannot be checked against anything.

So every event that changes what the desk owns or owes is ALSO appended here, as
one JSON line, to a file the database cannot corrupt and this process only ever
opens for append. It is not queryable and does not need to be. It needs to exist
when the database does not.

TAMPER-EVIDENT, DELIBERATELY

Each line carries the SHA-256 of the previous line. Truncate the file, edit a
number, drop a row, and every subsequent hash stops matching. That turns "trust
me" into "check it" -- which, after losing a day of records, is the only useful
thing to offer.

NEVER LOAD-BEARING

A journal failure must never take down a trade. Every write is wrapped: if the
disk is full or the path is gone, the trade still happens and the failure is
logged. A safety net that can drop the acrobat is not a safety net.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
# (file, hash) -- NOT a bare hash. The journal rolls to a new file at midnight,
# and a cached hash with no memory of which file it came from chained the first
# line of the new day onto the last line of the OLD one. verify() starts every
# file at GENESIS, so the guard reported "chain broken at line 1" every single
# day at midnight. Nothing was ever tampered with; the tamper alarm was.
#
# A guard that cries wolf on a schedule is a guard that gets ignored, which is
# worse than not having one.
_last: tuple[str, str] | None = None
GENESIS = "0" * 64


def _dir() -> Path:
    from app.config import get_settings
    d = Path(get_settings().db_file).parent / "journal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_for(ts: float) -> Path:
    return _dir() / f"{time.strftime('%Y-%m-%d', time.localtime(ts))}.jsonl"


def _tail_hash(p: Path) -> str:
    """The hash of the last line already in today's file, so a restart chains on."""
    try:
        with p.open("rb") as fh:
            last = b""
            for line in fh:
                if line.strip():
                    last = line.rstrip(b"\n")
        if last:
            return hashlib.sha256(last).hexdigest()
    except OSError:
        pass
    return GENESIS


def append(kind: str, payload: dict) -> bool:
    """Append one event. Returns False on failure; never raises."""
    global _last
    try:
        ts = time.time()
        p = _path_for(ts)
        with _LOCK:
            key = str(p)
            prev = (_last[1] if _last is not None and _last[0] == key
                    else _tail_hash(p))
            rec = {"ts": round(ts, 6), "kind": kind, "prev": prev, "data": payload}
            line = json.dumps(rec, separators=(",", ":"), sort_keys=True, default=str)
            raw = line.encode()
            with p.open("ab") as fh:
                fh.write(raw + b"\n")
                fh.flush()
                os.fsync(fh.fileno())      # survive a power cut, not just a crash
            _last = (key, hashlib.sha256(raw).hexdigest())
        return True
    except Exception as exc:
        try:
            from app.core import db
            db.log_event("ERROR", "journal", f"could not append {kind}: "
                                             f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        return False


def _prev_file_tail(path: Path) -> str | None:
    """The last hash of the journal file that came before this one."""
    try:
        fs = sorted(_dir().glob("*.jsonl"))
        i = fs.index(Path(path))
    except (OSError, ValueError):
        return None
    if i <= 0:
        return None
    return _tail_hash(fs[i - 1])


def verify(path: Path) -> dict:
    """Walk one journal file and confirm the hash chain is unbroken.

    A file may legitimately start EITHER from GENESIS or from the tail of the
    previous day's file: before 2026-09-19 the writer carried its cached hash
    across midnight, so the days written then are chained end to end. That is a
    real, verifiable chain -- just a different one -- and this is an append-only
    log, so the honest move is to teach the verifier what happened rather than
    rewrite the files so the old bug never appears to have existed.
    """
    ok, n, broke_at = True, 0, None
    prev = GENESIS
    carried = False
    try:
        with Path(path).open("rb") as fh:
            for i, line in enumerate(fh, 1):
                line = line.rstrip(b"\n")
                if not line.strip():
                    continue
                n += 1
                try:
                    rec = json.loads(line)
                except ValueError:
                    ok, broke_at = False, i
                    break
                if rec.get("prev") != prev:
                    # Line 1 only: accept a chain carried on from the previous
                    # day's file, and say so in the result.
                    if i == 1 and rec.get("prev") == _prev_file_tail(path):
                        prev, carried = rec["prev"], True
                    else:
                        ok, broke_at = False, i
                        break
                prev = hashlib.sha256(line).hexdigest()
    except OSError as exc:
        return {"ok": False, "lines": 0, "error": str(exc)}
    return {"ok": ok, "lines": n, "broke_at": broke_at,
            "chained_from_previous_day": carried}


def files() -> list[Path]:
    try:
        return sorted(_dir().glob("*.jsonl"))
    except OSError:
        return []
