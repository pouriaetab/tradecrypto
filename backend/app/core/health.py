"""Process health: memory, disk, and a ceiling that ends the process cleanly.

Why this exists: the first overnight run died. A macOS "Python quit unexpectedly"
dialog is what an out-of-memory kill looks like from the outside, and there was
an unbounded cache in the breakout module that fits the symptom exactly.

Two lessons are built in here.

1. A long-running process should watch its own memory and stop ITSELF when it
   crosses a line, rather than growing until the OS kills it. A clean exit is
   restartable; an OS kill loses whatever was in flight and tells you nothing.
2. A process that writes to disk forever needs a retention policy, or it becomes
   the thing that fills the machine.

Exit code 3 means "I stopped on purpose, please restart me" — the supervisor
(`scripts/com.tradecrypto.plist`) treats it as a normal restart rather than a
failure loop.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from app.config import get_settings
from app.core import db

MEMORY_EXIT_CODE = 3
_state = {"peak_mb": 0.0, "started": time.time(), "samples": [],
          "watch": False, "thread": None}


# ── memory ───────────────────────────────────────────────────────────────────
def rss_mb() -> float:
    """Current resident set size in MB. Works on macOS and Linux without psutil."""
    try:                                            # Linux
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    except Exception:
        pass
    try:                                            # macOS
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=5)
        return float(out.stdout.strip()) / 1024
    except Exception:
        return 0.0


def memory() -> dict:
    cur = rss_mb()
    _state["peak_mb"] = max(_state["peak_mb"], cur)
    s = get_settings()
    ceiling = float(getattr(s, "memory_ceiling_mb", 1000.0))
    # growth rate over the samples we have kept, in MB/hour
    growth = None
    if len(_state["samples"]) >= 2:
        (t0, m0), (t1, m1) = _state["samples"][0], _state["samples"][-1]
        if t1 > t0:
            growth = (m1 - m0) / ((t1 - t0) / 3600)
    return {
        "rss_mb": cur, "peak_mb": _state["peak_mb"], "ceiling_mb": ceiling,
        "pct_of_ceiling": 100 * cur / ceiling if ceiling else None,
        "growth_mb_per_hour": growth,
        "uptime_hours": (time.time() - _state["started"]) / 3600,
        "note": ("A steady rss is healthy. A growth rate that does not flatten means "
                 "something is accumulating — the process will stop itself at the "
                 "ceiling so a supervisor can restart it cleanly."),
    }


# macOS killed this process fifteen times before the watchdog ever fired once.
# Ten-second sampling cannot beat jetsam when a research job allocates hundreds
# of megabytes in two of them, and a clean exit-3 restart is strictly better
# than a SIGKILL: the ceiling path checkpoints the WAL on the way out, and an
# unflushed WAL is how this database got corrupted twice.
#
# But rss_mb() shells out to `ps` on macOS, so polling fast all day would spawn
# tens of thousands of subprocesses to learn nothing. Fast sampling only earns
# its keep while memory is already climbing, so the cadence follows the number:
# idle is cheap, and the approach to the ceiling is watched closely.
SLOW_SAMPLE_S = 15.0         # below 35% of the ceiling: nothing to watch
SAMPLE_SECONDS = 5.0         # 35-60%: normal
FAST_SAMPLE_S = 2.0          # past 60%: something is allocating, watch it
WARN_FRACTION = 0.75         # tell the log before the ceiling, not only at it
RESERVE_FRACTION = 0.15      # headroom kept free so the guard is never the cause


def headroom(estimate_mb: float = 0.0) -> dict:
    """Is there room to allocate `estimate_mb` without approaching the ceiling?

    Racing the ceiling is a losing game -- by the time the watchdog samples, the
    allocation has already happened and the operating system may have acted
    first. The heavy callers ask this BEFORE they allocate, and decline with a
    reason the operator can read instead of dying mid-job.
    """
    s = get_settings()
    ceiling = float(getattr(s, "memory_ceiling_mb", 1000.0))
    cur = rss_mb()
    room = ceiling - cur
    fits = (room - float(estimate_mb)) > ceiling * RESERVE_FRACTION
    return {
        "ok": bool(fits), "rss_mb": round(cur, 1), "ceiling_mb": ceiling,
        "room_mb": round(room, 1), "needs_mb": round(float(estimate_mb), 1),
        "reason": (None if fits else
                   f"{cur:.0f} MB in use of a {ceiling:.0f} MB ceiling; this step "
                   f"wants about {estimate_mb:.0f} MB more, which would leave less "
                   f"than the {RESERVE_FRACTION:.0%} reserve"),
    }


def panel_cost_mb(bars: int, symbols: int) -> float:
    """Rough peak cost of a bars x symbols panel, in MB.

    Five float64 matrices, plus the forward fill's temporaries, which measure
    at a little over half the matrices again.
    """
    arrays = 5.0 * float(bars) * float(symbols) * 8.0 / 1e6
    return arrays * 1.7


def _watch_loop() -> None:
    s = get_settings()
    ceiling = float(getattr(s, "memory_ceiling_mb", 1000.0))
    warned = False
    while _state["watch"]:
        cur = rss_mb()
        _state["peak_mb"] = max(_state["peak_mb"], cur)
        _state["samples"].append((time.time(), cur))
        if len(_state["samples"]) > 1440:            # a few hours of history
            _state["samples"] = _state["samples"][-1440:]

        if ceiling and cur > ceiling * WARN_FRACTION and not warned:
            warned = True
            db.log_event("WARN", "health",
                         f"memory {cur:.0f} MB is past {WARN_FRACTION:.0%} of the "
                         f"{ceiling:.0f} MB ceiling — whatever ran just before this "
                         f"is the thing that allocates")
        elif ceiling and cur < ceiling * 0.5:
            warned = False

        if ceiling and cur > ceiling:
            db.log_event("CRITICAL", "health",
                         f"memory {cur:.0f} MB exceeded the {ceiling:.0f} MB ceiling — "
                         f"exiting cleanly for the supervisor to restart")
            try:
                db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            os._exit(MEMORY_EXIT_CODE)

        frac = (cur / ceiling) if ceiling else 0.0
        time.sleep(FAST_SAMPLE_S if frac >= 0.60
                   else SAMPLE_SECONDS if frac >= 0.35
                   else SLOW_SAMPLE_S)


def start_watch() -> None:
    """Start the watchdog. Idempotent, and it verifies the probe works first.

    rss_mb() returning 0.0 would make the ceiling unreachable and the guard
    useless, so that is treated as a failure rather than started blindly.
    """
    if watch_alive():
        return
    probe = rss_mb()
    if probe <= 0:
        raise RuntimeError(
            "cannot read this process's memory usage on this platform; "
            "refusing to start a watchdog that could never fire")
    _state["watch"] = True
    _state["peak_mb"] = max(_state["peak_mb"], probe)
    t = threading.Thread(target=_watch_loop, daemon=True, name="tc-health")
    t.start()
    _state["thread"] = t


def watch_alive() -> bool:
    """Is the guard actually running right now? Not 'was it asked to start'."""
    t = _state.get("thread")
    return bool(_state.get("watch")) and t is not None and t.is_alive()


# ── disk ─────────────────────────────────────────────────────────────────────
def disk() -> dict:
    s = get_settings()
    dbf = s.db_file
    try:
        usage = shutil.disk_usage(dbf.parent)
        free_gb, total_gb = usage.free / 1e9, usage.total / 1e9
    except Exception:
        free_gb = total_gb = None
    sizes = {}
    for name, path in (("database", dbf), ("wal", dbf.with_name(dbf.name + "-wal"))):
        try:
            sizes[name + "_mb"] = path.stat().st_size / 1e6
        except OSError:
            sizes[name + "_mb"] = 0.0
    project_mb = sum(sizes.values())
    low = free_gb is not None and free_gb < 10
    return {
        **sizes, "project_mb": project_mb,
        "free_gb": free_gb, "total_gb": total_gb,
        "pct_used": (100 * (1 - free_gb / total_gb)) if free_gb and total_gb else None,
        "low_space": low,
        "warning": ("Under 10 GB free. macOS starts behaving badly well before zero, and "
                    "a database write that fails midway is how a file gets corrupted. "
                    "Free space or reduce retention." if low else None),
    }


def report() -> dict:
    return {"memory": memory(), "disk": disk(),
            "watchdog_alive": watch_alive(),
            "pid": os.getpid(), "python": sys.version.split()[0]}
