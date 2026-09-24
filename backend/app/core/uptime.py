"""Is the desk set up to survive — and is anything deliberately holding it down?

The operator's goal is a desk that runs 24 hours a day. Two things can quietly
defeat that, and neither was visible anywhere on screen:

  1. **Nothing is keeping it alive.** Without the LaunchAgent the desk only runs
     while the process manager runs it, and quitting that takes the desk with it.
     On 2026-09-18 the desk was stopped three times in an hour that way, and the
     UI's only symptom was a spinner.
  2. **It is deliberately paused.** data/STOP_SUPERVISOR stops launchd and the
     supervisor alike. A pause nobody can see is indistinguishable from a fault.

So both states are reported to the UI. Read-only; it never changes anything.
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from app.config import get_settings

LABEL = "com.tradecrypto.app"
PAUSE_NAME = "STOP_SUPERVISOR"


def _project_root() -> Path:
    return Path(get_settings().db_file).parent.parent


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def status() -> dict:
    root = _project_root()
    pause = root / "data" / PAUSE_NAME
    plist = _plist_path()

    out: dict = {
        "label": LABEL,
        "autostart_installed": plist.is_file(),
        "paused": pause.is_file(),
        "pause_file": str(pause),
        "plist": str(plist),
        "points_at": None,
        "stale_path": False,
        "loaded": False,
    }

    if not plist.is_file():
        out["summary"] = ("not installed — the desk only runs while Control Deck runs it, "
                          "and quitting Control Deck stops it")
        return out

    # Which project does the installed agent actually point at? If the folder was
    # renamed or moved, launchd goes on trying to run a path that is not here any
    # more, forever, and the desk never comes up. That is worth saying plainly.
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
        args = data.get("ProgramArguments") or []
        target = next((a for a in args if a.endswith("supervise.sh")), None)
        out["points_at"] = target
        if target:
            out["stale_path"] = not Path(target).is_file()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"

    try:
        r = subprocess.run(["launchctl", "print", f"gui/{__import__('os').getuid()}/{LABEL}"],
                           capture_output=True, timeout=5)
        out["loaded"] = r.returncode == 0
    except Exception:
        pass

    if out["stale_path"]:
        out["summary"] = (f"installed but pointing at {out['points_at']}, which is not there — "
                          f"the desk cannot start until it is reinstalled")
    elif out["paused"]:
        out["summary"] = ("paused on purpose — nothing will start the desk, not the autostart "
                          "agent and not Control Deck, until the pause is lifted")
    elif out["loaded"]:
        out["summary"] = "runs at login and restarts itself if it stops"
    else:
        out["summary"] = "installed but not loaded — it will take effect at the next login"
    return out
