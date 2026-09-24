"""Is the code on disk newer than the code that is running?

This cost four days. Changes shipped on 2026-09-13, -14 and -15 -- a new
strategy, a corrected spread model, a P&L repair, two new pages -- and the
process kept running a build from 2026-09-12 21:28. Every report in between was
measuring code that was not executing, and nothing in the app said so. The
Overview looked completely normal.

The check is cheap and blunt: record the newest source mtime at import, and
compare it with the newest source mtime now. A file saved after boot means the
running process is stale, full stop.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# ONLY BACKEND FILES COUNT.
#
# This used to watch frontend/src too, and it was wrong in a way that trained the
# operator to ignore the warning: vite hot-reloads .jsx and .css the instant they
# change, so the browser is already running the new code. Reporting "restart
# needed — styles.css is 0.8h newer than the running build" is false, and a
# warning that is usually false is a warning nobody reads when it is true.
#
# Python is different. The backend loads its modules once at import, so an edit
# to a .py file genuinely does nothing until the process restarts. That is the
# case worth interrupting someone about, and now it is the only one.
WATCH_SUFFIXES = (".py",)
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "data", "logs", "dist", ".git"}

_ROOT = Path(__file__).resolve().parents[3]      # repo root
_BOOT_TS = time.time()
_BOOT_NEWEST: float = 0.0
_BOOT_FILE: str = ""


def _newest() -> tuple[float, str]:
    newest, where = 0.0, ""
    # .env is read once, at boot. 2026-09-23: the operator raised
    # TC_MAX_DAILY_LOSS_PCT from 3 to 8 at 16:51 and the desk kept tripping the
    # $60 cap until 18:51, because the running process had never read the new
    # file and nothing said so. A settings edit is a code change for this
    # purpose: it is inert until the restart, and the same planned restart
    # applies it.
    try:
        env = _ROOT / ".env"
        if env.exists():
            newest, where = os.path.getmtime(env), ".env"
    except OSError:
        pass
    for base in (_ROOT / "backend" / "app",):
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(WATCH_SUFFIXES):
                    continue
                try:
                    m = os.path.getmtime(os.path.join(dirpath, fn))
                except OSError:
                    continue
                if m > newest:
                    newest, where = m, os.path.relpath(os.path.join(dirpath, fn), _ROOT)
    return newest, where


def snapshot_boot() -> None:
    global _BOOT_NEWEST, _BOOT_FILE
    _BOOT_NEWEST, _BOOT_FILE = _newest()


def status() -> dict:
    newest, where = _newest()
    stale = bool(_BOOT_NEWEST and newest > _BOOT_NEWEST + 1.0)
    return {
        "booted_at": _BOOT_TS,
        "uptime_h": round((time.time() - _BOOT_TS) / 3600, 2),
        "newest_source_at_boot": _BOOT_NEWEST or None,
        "newest_source_now": newest or None,
        "newest_source_file": where or None,
        "code_changed_since_boot": stale,
        "hours_stale": (round((newest - _BOOT_NEWEST) / 3600, 2)
                        if stale and _BOOT_NEWEST else 0.0),
        "message": (
            f"Source changed after this process started ({where} is "
            f"{round((newest - _BOOT_NEWEST) / 3600, 1)}h newer than the running build). "
            f"Restart to pick it up — until then the app is measuring code it is not running."
            if stale else "running the newest code on disk"),
    }
