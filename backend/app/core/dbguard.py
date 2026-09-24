"""Cross-host mutual exclusion for the SQLite file.

SQLite's own locking is POSIX advisory locks, and those do not survive a bridge
or network mount: a process reading the file through a share on Linux and a
process holding it open natively on macOS consult *different* lock tables, so
neither one ever blocks the other. Two unsynchronised writers is the textbook
way to destroy a SQLite database, and on 2026-09-18 that is precisely what
happened here -- the 450 MB file came back with no SQLite header anywhere in
it, and eighteen hours of trading had to be restored from the nightly backup.

The fix cannot be a better lock, because no lock primitive crosses that mount.
It has to be built out of the one thing that does: an ordinary file. Every
process that opens the database read-write writes a claim naming its host, OS
and pid, then refreshes a heartbeat for as long as it holds it. A process that
finds a *live* claim belonging to a different machine refuses to open
read-write at all, and says why.

Deliberately asymmetric:

  * A stale claim from THIS host whose pid is gone is taken over immediately,
    so the supervisor's crash-restart loop is never delayed by its own corpse.
  * A claim from ANOTHER host is honoured until its heartbeat goes stale, no
    matter how tempting, because we cannot see whether that pid is alive and
    guessing wrong is what corrupts the file.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from dataclasses import dataclass, asdict, replace
from pathlib import Path

# Heartbeat cadence, and how long a foreign claim is believed after its last
# beat. Four missed beats: long enough to ride out a slow mount or a paused VM,
# short enough that a genuinely dead foreign process frees the database before
# anyone gets impatient enough to delete the sentinel by hand.
BEAT_EVERY_S = 20.0
STALE_AFTER_S = 90.0

# After writing our claim, re-read it. Two processes that write at the same
# instant both succeed -- last writer wins -- and only the re-read reveals which
# one that was. The pause is there so the loser's write has landed before the
# winner looks.
CONFIRM_PAUSE_S = 0.35

SENTINEL_NAME = ".db-owner.json"


class DatabaseInUse(RuntimeError):
    """Another machine holds this database read-write."""


@dataclass(frozen=True)
class Claim:
    host: str
    system: str
    pid: int
    role: str
    since: float
    heartbeat: float

    @property
    def machine(self) -> str:
        return f"{self.host}/{self.system}"

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.heartbeat)

    def is_stale(self, now: float | None = None) -> bool:
        return self.age_s(now) > STALE_AFTER_S

    def describe(self) -> str:
        return (f"{self.role} on {self.machine} (pid {self.pid}), "
                f"last seen {self.age_s():.0f}s ago")


def _me(role: str) -> Claim:
    now = time.time()
    return Claim(host=platform.node() or "unknown",
                 system=platform.system() or "unknown",
                 pid=os.getpid(), role=role, since=now, heartbeat=now)


def _same_machine(a: Claim, b: Claim) -> bool:
    return a.host == b.host and a.system == b.system


def _is_zombie(pid: int) -> bool:
    """Has this pid exited without being reaped?

    webapp_blueprint CHECKLIST 1.6: `kill -0` succeeds on a ZOMBIE -- a process
    that has exited but whose parent has not reaped it -- so a crashed app reads
    as alive. Here that means a dead desk keeps its claim on the database and the
    next start is refused until the heartbeat goes stale, which is a minute and a
    half of an app that will not come up for no reason.
    """
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return False         # cannot tell => do not call a live process dead
    state = (out.stdout or "").strip()
    return state.startswith("Z")


def _pid_alive(pid: int) -> bool:
    """Only meaningful for a pid on this host. Never call it on a foreign claim."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return True          # unknown: assume alive, the safer answer
    # kill(0) succeeded -- but that is also true of a zombie (CHECKLIST 1.6).
    return not _is_zombie(pid)


def sentinel_path(db_file: Path) -> Path:
    return Path(db_file).parent / SENTINEL_NAME


def read_claim(db_file: Path) -> Claim | None:
    p = sentinel_path(db_file)
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    try:
        return Claim(host=str(raw["host"]), system=str(raw["system"]),
                     pid=int(raw["pid"]), role=str(raw.get("role", "?")),
                     since=float(raw["since"]), heartbeat=float(raw["heartbeat"]))
    except (KeyError, TypeError, ValueError):
        return None          # unreadable sentinel is no sentinel


def _write_claim(db_file: Path, c: Claim) -> None:
    p = sentinel_path(db_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(c)))
    os.replace(tmp, p)       # atomic within one filesystem


def _blocking_claim(db_file: Path, mine: Claim, now: float) -> Claim | None:
    """The claim that forbids us the database, or None if we may proceed."""
    held = read_claim(db_file)
    if held is None:
        return None
    if held.pid == mine.pid and _same_machine(held, mine):
        return None                              # our own claim
    if _same_machine(held, mine):
        # Same host: we can ask the OS directly whether that process still
        # exists, which beats waiting out a heartbeat on a crashed sibling.
        # Both conditions, though -- the OS recycles pids, and a dead app whose
        # number has been handed to something unrelated would otherwise lock
        # the desk out of its own database until somebody deleted the file.
        return held if (_pid_alive(held.pid) and not held.is_stale(now)) else None
    # Foreign host: the pid is meaningless to us. Trust only the heartbeat.
    return None if held.is_stale(now) else held


def claim(db_file: Path, role: str = "app", *, confirm: bool = True) -> Claim:
    """Take the database read-write, or raise DatabaseInUse explaining who has it."""
    db_file = Path(db_file)
    mine = _me(role)
    now = time.time()

    blocker = _blocking_claim(db_file, mine, now)
    if blocker is not None:
        raise DatabaseInUse(
            f"{db_file.name} is held read-write by {blocker.describe()}; this "
            f"process is {mine.role} on {mine.machine} (pid {mine.pid}). Two "
            f"writers on different machines is how this file gets corrupted, so "
            f"this process will not open it. Read it WITHOUT TOUCHING IT "
            f"instead -- sqlite3.connect('file:{db_file.name}?immutable=1', "
            f"uri=True) -- or work against a copy in data/ledger/. Do not use "
            f"mode=ro on its own from another machine: a WAL reader still maps "
            f"and writes the -shm index, which is what put a 3-second burst of "
            f"'file is not a database' through a healthy desk on 2026-09-18.")

    _write_claim(db_file, mine)
    if confirm:
        # Someone may have written between our check and our write. Whoever
        # landed last owns the file; if that is not us, we back off.
        time.sleep(CONFIRM_PAUSE_S)
        landed = read_claim(db_file)
        if landed is not None and not (landed.pid == mine.pid
                                       and _same_machine(landed, mine)):
            raise DatabaseInUse(
                f"lost a race for {db_file.name} to {landed.describe()}; not "
                f"opening it read-write.")
    return mine


def beat(db_file: Path, role: str = "app") -> bool:
    """Refresh our heartbeat. False if we no longer hold the claim."""
    db_file = Path(db_file)
    mine = _me(role)
    held = read_claim(db_file)
    if held is not None and not (held.pid == mine.pid and _same_machine(held, mine)):
        return False
    if held is not None:
        # Carry the ORIGINAL claim time forward. A heartbeat says "still here",
        # not "just arrived". Rebuilding the claim from scratch every 20s reset
        # `since` to now, so the file always read as a brand-new claim and the
        # one field that tells you whether the app has been up continuously was
        # destroyed by the very loop that proves it is alive. (2026-09-18: this
        # made a healthy 25-minute-old desk look like it had just restarted.)
        mine = replace(mine, since=held.since)
    _write_claim(db_file, mine)
    return True


def release(db_file: Path) -> None:
    """Give the database up. Only ever removes our own claim."""
    db_file = Path(db_file)
    mine = _me("")
    held = read_claim(db_file)
    if held is None:
        return
    if held.pid == mine.pid and _same_machine(held, mine):
        try:
            sentinel_path(db_file).unlink()
        except OSError:
            pass


def holder(db_file: Path) -> Claim | None:
    """Whoever currently holds it, live claims only. For status pages."""
    held = read_claim(Path(db_file))
    if held is None or held.is_stale():
        return None
    return held


def start_heartbeat(db_file: Path, role: str = "app") -> None:
    """Keep our claim alive for as long as this process runs.

    A daemon thread, deliberately: it must not be able to hold the process open
    at shutdown, and it must not depend on the scheduler, whose shortest job
    interval is an hour and which is itself one of the things that could wedge.
    """
    import threading

    def _loop() -> None:
        while True:
            try:
                if not beat(db_file, role):
                    return          # someone else took it; stop pretending
            except Exception:
                pass                # a missed beat is survivable, a crash is not
            time.sleep(BEAT_EVERY_S)

    t = threading.Thread(target=_loop, name="db-heartbeat", daemon=True)
    t.start()
