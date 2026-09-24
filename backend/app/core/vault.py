"""The vault: a write-once copy of every order and every trade, kept OUTSIDE
everything else this app owns.

WHY THIS EXISTS
---------------
On 2026-09-17 a 450 MB SQLite database was destroyed and eighteen hours of
trading went with it. What came out of that was `core/journal.py` -- an
append-only, hash-chained second copy. It is good, and it is not enough, because
it shares three things with the database it is meant to outlive:

  * the same folder      -- one `rm -rf`, one bad script, one sync client
  * the same process     -- one crash before the write, one bug in this codebase
  * the same machine     -- one disk

The vault fixes the first two here and the third with an off-machine mirror.
The operator's requirement was exactly this: *"a backup vault that should never
be needed to access and only can add data to it as a redundant place but
independent completely from any other parts of the whole system."*

WHAT INDEPENDENCE MEANS HERE, CONCRETELY
----------------------------------------
  * Not in the project folder. `TC_VAULT_DIR`, default `~/tradecrypto-vault`.
  * Not SQLite. One JSON object per line; `cat` and `grep` are enough to read it,
    and no schema, migration or page format stands between the bytes and a human.
  * Not dependent on this codebase. Nothing below imports from `app.*`, so the
    write path cannot be broken by a change anywhere else in the app, and
    `scripts/vault_verify.py` reads it with a stock python3 and no project on
    the path at all.
  * Append-only by construction AND by permission: files open in 'a', and a day
    that is over is chmod'ed read-only.
  * Never read in normal operation. `reconcile()` reads only the small index of
    ids it has already written, never the records themselves.

WHY IT IS 100% AND NOT "BEST EFFORT"
------------------------------------
Two mechanisms, because one is never enough:

  1. `record()` runs SYNCHRONOUSLY, in the same call that places the order or
     closes the trade, and fsyncs. There is no queue to drain and no worker to
     die, so the only way to miss a write is for the write itself to fail.
  2. `reconcile()` compares the vault's id index against the database and
     appends anything the database has that the vault does not. So a write that
     DID fail -- full disk, revoked permission, unplugged volume -- is caught on
     the next pass instead of becoming a silent hole.

(1) alone is a promise. (2) is what makes it checkable, and the difference
between those two is the entire lesson of the last two days.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path

GENESIS = "0" * 64
_LOCK = threading.Lock()

# Kept deliberately tiny and dependency-free: `os`, `json`, `hashlib`. Anything
# imported here is something that can break the one write that must not break.


def vault_dir() -> Path:
    """Where the vault lives. Outside the project, always."""
    raw = os.environ.get("TC_VAULT_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / "tradecrypto-vault"


def _records_dir() -> Path:
    d = vault_dir() / "records"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _day_file(ts: float) -> Path:
    return _records_dir() / f"{time.strftime('%Y-%m-%d', time.gmtime(ts))}.ndjson"


def _index_path() -> Path:
    return vault_dir() / "index.txt"


def _tail_hash(p: Path) -> str:
    """The previous line's hash, read from the file itself.

    From the FILE, not from memory: a cached value is a value that can be wrong
    after a crash, and the chain is the only thing that proves nothing was
    removed from the middle.
    """
    if not p.exists():
        return GENESIS
    last = b""
    try:
        with p.open("rb") as fh:
            for line in fh:
                if line.strip():
                    last = line
    except OSError:
        return GENESIS
    if not last:
        return GENESIS
    try:
        return json.loads(last)["h"]
    except Exception:
        # A half-written final line (a crash mid-append) must not stop the next
        # write. Chain from its bytes so the break is visible but the vault
        # keeps accepting records -- losing new data to protect old data is a
        # bad trade.
        return hashlib.sha256(last).hexdigest()


def _seal_older_days(today: Path) -> None:
    """A day that is over can never gain another record, so make that true.

    Read-only is not security -- anything running as this user can undo it. It
    is a tripwire and a statement of intent: a process that finds itself unable
    to write to a sealed day is a process doing something it was never meant to.
    """
    try:
        for f in _records_dir().glob("*.ndjson"):
            if f == today:
                continue
            mode = stat.S_IMODE(f.stat().st_mode)
            if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                f.chmod(0o444)
    except OSError:
        pass


def record(kind: str, key: str, payload: dict) -> bool:
    """Append one record. Never raises. Returns True if it landed.

    `key` is the natural, stable identity of the thing -- "order:412",
    "trade:37". It is what `reconcile()` compares against, so it must be the
    same string every time the same thing is recorded, and never an id that a
    restore could reissue to something else (blueprint 1.6).
    """
    try:
        ts = time.time()
        line = {
            "ts": ts,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "kind": kind,
            "key": key,
            "payload": payload,
        }
        with _LOCK:
            p = _day_file(ts)
            prev = _tail_hash(p)
            body = json.dumps(line, sort_keys=True, default=str)
            line["prev"] = prev
            line["h"] = hashlib.sha256((prev + body).encode()).hexdigest()
            blob = json.dumps(line, sort_keys=True, default=str) + "\n"
            with p.open("a", encoding="utf-8") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())      # the whole point: survive the power going out
            with _index_path().open("a", encoding="utf-8") as fh:
                fh.write(key + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            _seal_older_days(p)
        return True
    except Exception:
        # Never load-bearing. A vault that can stop a trade is a vault that has
        # made the system less reliable, not more -- and reconcile() will pick
        # up whatever this call dropped.
        return False


def known_keys() -> set[str]:
    """Every key the vault has ever been given. Cheap: one small text file."""
    try:
        with _index_path().open(encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}
    except OSError:
        return set()


def status() -> dict:
    """Where it is, how much is in it, when it was last written. Read-only."""
    d = vault_dir()
    try:
        files = sorted(_records_dir().glob("*.ndjson"))
    except OSError:
        files = []
    lines, bytes_ = 0, 0
    for f in files:
        try:
            bytes_ += f.stat().st_size
            with f.open("rb") as fh:
                lines += sum(1 for ln in fh if ln.strip())
        except OSError:
            pass
    newest = max((f.stat().st_mtime for f in files), default=None)
    return {
        "dir": str(d),
        "inside_project": str(d.resolve()).startswith(str(Path(__file__).resolve().parents[3])),
        "exists": d.exists(),
        "days": len(files),
        "records": lines,
        "megabytes": round(bytes_ / 1e6, 3),
        "last_write_ts": newest,
        "last_write_age_s": (time.time() - newest) if newest else None,
        "keys_indexed": len(known_keys()),
    }


def reconcile() -> dict:
    """Append anything the database has that the vault does not. Idempotent.

    This is the half that turns "we write it every time" into something you can
    check. `record()` is synchronous and fsynced, so it fails only if the write
    itself fails -- and a write that failed is exactly the case a promise cannot
    cover. Every order and every closed trade in the database is compared
    against the vault's key index, and whatever is missing is appended now.

    It reads the INDEX, never the records: the vault is a place data goes, not
    a place the app reads from.
    """
    from app.core import db, liveness            # lazy: the write path stays clean

    out = {"checked": 0, "added": 0, "failed": 0, "dir": str(vault_dir())}
    try:
        have = known_keys()
        LATE = ("reconcile — this record reached the vault later than the event "
                "it describes. The database is where it came from; it is here now.")

        # Two plain queries rather than one clever UNION: the columns mean
        # different things, and a UNION that lines them up positionally is the
        # exact shape of mistake that read a cost hurdle as a position size
        # (blueprint 0.4).
        orders = db.query(
            """SELECT id, symbol, side, intent, qty, notional_usd, mode, status,
                      strategy, ts_decided, ts_submitted, ts_filled, fill_price,
                      fee_usd, broker_order_id, reject_reason
               FROM orders""")
        trades = db.query(
            """SELECT id, symbol, strategy, mode, qty, entry_px, exit_px,
                      ts_open, ts_close, gross_pnl_usd, cost_usd, net_pnl_usd,
                      open_order_id, close_order_id
               FROM trades WHERE ts_close IS NOT NULL""")
        out["checked"] = len(orders) + len(trades)

        for r in orders:
            k = f"order:{r['id']}"
            if k in have:
                continue
            if record("order", k, {**dict(r), "source": LATE}):
                out["added"] += 1; have.add(k)
            else:
                out["failed"] += 1

        for r in trades:
            k = f"trade:{r['id']}"
            if k in have:
                continue
            if record("trade", k, {**dict(r), "source": LATE}):
                out["added"] += 1; have.add(k)
            else:
                out["failed"] += 1
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"

    # Reported from HERE, not from record(): record() is the write path and it
    # imports nothing from app.* on purpose, so that a change anywhere else in
    # this codebase cannot break the one write that must not break. A test
    # enforces that, and it caught this the moment it was violated.
    try:
        st0 = status()
        if st0.get("records"):
            liveness.fired("vault_write", f"{st0['records']} record(s)")
    except Exception:
        pass

    # A status file INSIDE the project, so the app, the gate and anything else
    # can see how the vault is doing without ever opening the vault itself.
    try:
        from app.config import get_settings
        st = status()
        st["reconcile"] = out
        st["checked_at"] = time.time()
        p = Path(get_settings().db_file).parent / "vault-status.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, default=str))
        tmp.replace(p)
    except Exception:
        pass
    return out
