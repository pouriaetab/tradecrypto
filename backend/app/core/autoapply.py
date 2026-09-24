"""Pick up new backend code by itself, at a moment when it is safe to.

THE PROBLEM THIS SOLVES

Python loads its modules once. Every backend change sits inert until the process
restarts, so the working loop was: edit, remember to stop, remember to start,
and hope you remembered. The operator's words: *"my ideal scenario is to never
close or open stop or restart and have it all automated as much as possible."*

The pieces already existed and were never connected:

  * `code_version` knows the source on disk is newer than the running build --
    it was written after this app spent four days executing a build from three
    days earlier while every screen looked normal.
  * `supervise.sh` already treats **exit code 3** as a PLANNED restart: no
    backoff, no "failed start" counter, straight back up.

So the engine exits 3 on its own once the code is stale AND it is safe to go.

WHEN IT IS SAFE, AND WHY EACH CONDITION IS THERE

  * **No order in flight.** A restart between "submitted" and "filled" is how you
    lose track of real money. Nothing else on this list is negotiable either, but
    this is the one that would cost.
  * **The edits have settled.** A file saved 4 seconds ago is probably one of
    several being saved. Restarting mid-edit runs half a change. It waits until
    the newest source file has been still for a while.
  * **The process has been up a while.** Without this, a clock skew or an odd
    mtime could restart the desk in a loop at boot.

It is self-limiting by construction: after the restart the running build IS the
newest build, so the condition clears itself. No cooldown state to keep.
"""
from __future__ import annotations

import os
import time

from app.core import code_version, db, journal

# Env overrides exist so this can be turned off without editing code; the
# defaults are what the desk actually runs on.
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    return (os.environ.get("TC_AUTO_APPLY_CODE", "1").strip().lower()
            not in {"0", "false", "no", "off"})


SETTLE_S = _f("TC_AUTO_APPLY_SETTLE_S", 90.0)      # edits must be this still
MIN_UPTIME_S = _f("TC_AUTO_APPLY_MIN_UPTIME_S", 120.0)
# supervise.sh exit-code table: 3 memory ceiling, 4 port in use, 5 TCC blocked,
# 6 new code. Sharing 3 made every code update read as a memory-ceiling restart.
RESTART_EXIT_CODE = 6


def orders_in_flight() -> int:
    try:
        row = db.query_one("SELECT COUNT(*) c FROM orders "
                           "WHERE status IN ('pending','submitted')")
        return int(row["c"]) if row else 0
    except Exception:
        return 1        # cannot tell => assume busy => do not restart


def check() -> dict:
    """Should the desk restart itself right now, and if not, why not."""
    st = code_version.status()
    out = {"enabled": enabled(), "stale": bool(st.get("code_changed_since_boot")),
           "file": st.get("newest_source_file"), "restart": False, "why": ""}

    if not out["enabled"]:
        out["why"] = "auto-apply is off (TC_AUTO_APPLY_CODE=0)"
        return out
    if not out["stale"]:
        out["why"] = "running the newest code"
        return out

    up = time.time() - float(st.get("booted_at") or 0)
    if up < MIN_UPTIME_S:
        out["why"] = f"only up {up:.0f}s — waiting for {MIN_UPTIME_S:.0f}s"
        return out

    still = time.time() - float(st.get("newest_source_now") or 0)
    if still < SETTLE_S:
        out["why"] = f"last edit {still:.0f}s ago — waiting for edits to settle"
        return out

    flight = orders_in_flight()
    if flight:
        out["why"] = f"{flight} order(s) in flight — will restart once they settle"
        return out

    out["restart"] = True
    out["why"] = f"new backend code ({out['file']}) and nothing in flight"
    return out


def apply_now(reason: str) -> None:
    """Hand the process back to the supervisor so it comes up on the new code."""
    msg = f"restarting to pick up new backend code — {reason}"
    try:
        db.log_event("INFO", "system", msg)
    except Exception:
        pass
    journal.append("code_auto_applied", {"reason": reason, "pid": os.getpid()})
    try:
        from app.core import liveness
        liveness.fired("auto_apply_restart", reason[:180])
    except Exception:
        pass
    # Stamp the moment we hand back, so the next boot can say how long the desk
    # was actually down. "Limit the downtime" is only a goal if it is measured.
    try:
        from app.core import mode as _mode
        _mode._ensure()
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES "
                   "('planned_shutdown_ts', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                   "value=excluded.value, updated_ts=excluded.updated_ts",
                   (str(time.time()), time.time()))
    except Exception:
        pass
    try:
        from app.config import get_settings
        from app.core import dbguard
        # Leave the database tidy: checkpoint what is in the WAL and give up the
        # claim, so the next process does not have to wait one out.
        try:
            db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        dbguard.release(get_settings().db_file)
    except Exception:
        pass
    print(f"[autoapply] {msg}", flush=True)
    os._exit(RESTART_EXIT_CODE)
