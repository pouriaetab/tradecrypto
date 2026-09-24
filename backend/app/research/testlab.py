"""The Test Lab — run every check yourself, and add your own.

Why this exists
---------------
Four bugs in this project were dangerous specifically because they were SILENT:
a memory watchdog that never started, a false-breakout model trained on 8% of
its data, a supervisor that read a crash as a clean shutdown, and a cost hurdle
below the real spread. None raised. None logged. Each looked healthy on a
dashboard while being broken underneath.

The lesson is that a green dashboard is a claim, not evidence. This module makes
the evidence runnable on demand: every test, grouped by the feature it covers,
executable one at a time or all at once, from the UI, with the raw output shown
rather than summarised into a tick.

Three layers, deliberately separate
-----------------------------------
1. SUITE tests      -- the project's own pytest files. What the authors assert.
2. INDEPENDENT      -- `verify.py`. Re-derives the same claims by a DIFFERENT
                       method, deliberately not sharing code with what it checks.
                       A model grading its own homework proves nothing.
3. YOUR tests       -- anything you drop in tests/user/. Same runner, same
                       reporting, never overwritten by an update.

Layer 2 is the one that matters most. If `auc()` had a bug, every test that
called `auc()` would agree with it. So the independent layer recomputes AUC from
the rank-sum identity, checks look-ahead by scrambling the future rather than by
trusting a flag, and confirms that a model's skill collapses when its labels are
shuffled — the check a model cannot pass by accident.
"""
from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from app.core import db

BACKEND = Path(__file__).resolve().parents[2]
TESTS = BACKEND / "tests"
USER_TESTS = TESTS / "user"
TIMEOUT_S = 900

# Which feature each test file speaks for. A test nobody can map to a feature is
# a test nobody will look at when that feature misbehaves.
FEATURES: dict[str, dict] = {
    "breakout": {
        "title": "False-breakout veto",
        "what": "Decides whether a level break is real or a trap. The veto that "
                "was silently trained on 454 events instead of 35,000.",
        "files": ["test_breakout.py"],
        "code": "backend/app/research/breakout.py",
    },
    "cost": {
        "title": "Execution cost and the hurdle",
        "what": "Robinhood's published spread, the exact round-trip break-even, "
                "and the number every signal must beat. The largest single term "
                "in whether any of this is profitable.",
        "files": ["test_core.py"],
        "code": "backend/app/execution/rh_spread.py, cost_model.py, symbol_cost.py",
    },
    "strategies": {
        "title": "The four strategies",
        "what": "fast flip, forced momentum, regime swing, lead-lag rotation — "
                "signal generation and the acceptance gates they must pass.",
        "files": ["test_operator_strategies.py"],
        "code": "backend/app/strategy/",
    },
    "operations": {
        "title": "Staying alive",
        "what": "Memory watchdog, retention, the supervisor's restart logic, and "
                "the guards that stop real money moving by accident.",
        "files": ["test_operations.py", "test_health.py", "test_silent_failures.py"],
        "code": "backend/app/core/health.py, housekeeping.py, scripts/supervise.sh",
    },
}


# ── discovery ────────────────────────────────────────────────────────────────
def _tests_in(path: Path) -> list[dict]:
    """Test functions and their docstrings, read from source rather than by
    importing — discovery must work even when a module is too broken to import."""
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except Exception as exc:
        return [{"name": "<unparseable>", "doc": f"{type(exc).__name__}: {exc}",
                 "line": 0, "broken": True}]
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_"):
            doc = ast.get_docstring(n) or ""
            out.append({"name": n.name, "line": n.lineno,
                        "doc": re.sub(r"\s+", " ", doc).strip(),
                        "node_id": f"{path.name}::{n.name}" if path.parent == TESTS
                                   else f"user/{path.name}::{n.name}"})
    return sorted(out, key=lambda t: t["line"])


def catalogue() -> dict:
    """Every test, grouped by the feature it covers."""
    features = []
    claimed: set[str] = set()
    for key, meta in FEATURES.items():
        files = []
        for fname in meta["files"]:
            p = TESTS / fname
            claimed.add(fname)
            if not p.exists():
                files.append({"file": fname, "missing": True, "tests": []})
                continue
            files.append({"file": fname, "missing": False, "tests": _tests_in(p)})
        features.append({**meta, "key": key, "files": files,
                         "n_tests": sum(len(f["tests"]) for f in files)})

    # Anything not claimed above still gets shown — an orphan test file is a
    # gap in this map, not a reason to hide the tests.
    orphans = []
    for p in sorted(TESTS.glob("test_*.py")):
        if p.name not in claimed:
            orphans.append({"file": p.name, "missing": False, "tests": _tests_in(p)})
    if orphans:
        features.append({"key": "other", "title": "Not yet mapped to a feature",
                         "what": "These tests exist but are not linked to a feature above.",
                         "code": "", "files": orphans,
                         "n_tests": sum(len(f["tests"]) for f in orphans)})

    USER_TESTS.mkdir(parents=True, exist_ok=True)
    mine = [{"file": p.name, "missing": False, "tests": _tests_in(p)}
            for p in sorted(USER_TESTS.glob("test_*.py"))]
    return {
        "features": features,
        "your_tests": {"key": "user", "title": "Your tests",
                       "what": "Anything you write in tests/user/. Run with the same "
                               "runner, never touched by an update.",
                       "files": mine,
                       "n_tests": sum(len(f["tests"]) for f in mine)},
        "independent": independent_catalogue(),
        "total": sum(f["n_tests"] for f in features) + sum(len(m["tests"]) for m in mine),
    }


def independent_catalogue() -> dict:
    from app.research import verify
    return {
        "key": "independent", "title": "Independent cross-check",
        "what": ("Re-derives the same claims by a different method, sharing no code "
                 "with what it checks. A model grading its own homework proves nothing."),
        "checks": verify.catalogue(),
    }


# ── running ──────────────────────────────────────────────────────────────────
def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS test_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
        passed INTEGER, failed INTEGER, errors INTEGER, skipped INTEGER,
        duration_s REAL, exit_code INTEGER, output TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_test_runs_ts ON test_runs(ts DESC)")


_SUMMARY = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed)")


def _python() -> str:
    venv = BACKEND / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def run(target: str = "") -> dict:
    """Run pytest. `target` is a node id, a file, or empty for everything.

    The full output is stored and shown. A test result compressed to a green tick
    is exactly the kind of reassurance that hid the last four bugs.
    """
    ensure_schema()
    if ".." in target or target.startswith("/"):
        return {"error": "target must be a test file or node id inside tests/"}
    argv = [_python(), "-m", "pytest", "-v", "--tb=short", "-p", "no:cacheprovider"]
    argv.append(str(TESTS / target) if target else str(TESTS))
    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(BACKEND), capture_output=True,
                              text=True, timeout=TIMEOUT_S)
        out, code = proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        out, code = f"timed out after {TIMEOUT_S}s", -1
    except FileNotFoundError as exc:
        out, code = f"cannot run pytest: {exc}", -1
    dur = time.time() - t0

    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for tail in out.strip().splitlines()[-6:]:
        for n, word in _SUMMARY.findall(tail):
            key = "errors" if word.startswith("error") else word
            if key in counts:
                counts[key] = max(counts[key], int(n))

    db.execute(
        """INSERT INTO test_runs(ts,target,kind,passed,failed,errors,skipped,
                                 duration_s,exit_code,output) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), target or "ALL", "suite", counts["passed"], counts["failed"],
         counts["errors"], counts["skipped"], dur, code, out[-200000:]))
    return {"target": target or "ALL", **counts, "duration_s": dur,
            "exit_code": code, "ok": code == 0, "output": out[-200000:]}


def run_independent(check: str = "") -> dict:
    from app.research import verify
    ensure_schema()
    t0 = time.time()
    res = verify.run(check or None)
    dur = time.time() - t0
    passed = sum(1 for r in res["checks"] if r["ok"])
    failed = sum(1 for r in res["checks"] if not r["ok"])
    db.execute(
        """INSERT INTO test_runs(ts,target,kind,passed,failed,errors,skipped,
                                 duration_s,exit_code,output) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), check or "ALL", "independent", passed, failed, 0, 0, dur,
         0 if failed == 0 else 1, json.dumps(res)[:200000]))
    return {**res, "duration_s": dur, "passed": passed, "failed": failed,
            "ok": failed == 0}


def history(limit: int = 40) -> list[dict]:
    ensure_schema()
    rows = db.query(
        """SELECT id, ts, target, kind, passed, failed, errors, skipped,
                  duration_s, exit_code FROM test_runs ORDER BY ts DESC LIMIT ?""",
        (limit,))
    return rows


def output(run_id: int) -> dict:
    ensure_schema()
    row = db.query_one("SELECT * FROM test_runs WHERE id=?", (run_id,))
    return row or {"error": f"no run {run_id}"}


# ── your own tests ───────────────────────────────────────────────────────────
SAFE_NAME = re.compile(r"^test_[a-z0-9_]{1,60}\.py$")


def save_user_test(filename: str, source: str) -> dict:
    """Write a test file into tests/user/. Rejected if it does not parse — a
    syntax error saved now is a confusing pytest collection error later."""
    if not SAFE_NAME.match(filename or ""):
        return {"error": "filename must look like test_my_check.py (lowercase, no spaces)"}
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return {"error": f"line {exc.lineno}: {exc.msg}"}
    USER_TESTS.mkdir(parents=True, exist_ok=True)
    (USER_TESTS / "__init__.py").touch(exist_ok=True)
    path = USER_TESTS / filename
    io.open(path, "w", encoding="utf-8").write(source)
    return {"saved": f"tests/user/{filename}", "tests": _tests_in(path)}


def read_user_test(filename: str) -> dict:
    if not SAFE_NAME.match(filename or ""):
        return {"error": "bad filename"}
    path = USER_TESTS / filename
    if not path.exists():
        return {"error": "not found"}
    return {"filename": filename, "source": io.open(path, encoding="utf-8").read()}


TEMPLATE = '''"""Your own checks. This file is never overwritten by an update.

Anything importable from the app is available:

    from app.execution import rh_spread, cost_model
    from app.research import breakout
    from app.strategy.base import Panel

A test is any function whose name starts with test_. It passes if it does not
raise, so `assert` is the whole vocabulary you need.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.execution import rh_spread


def test_the_round_trip_costs_what_the_ticket_says():
    """DOGE ticket: mid 0.090569, buy 0.091485, bid 0.089704 -> 0.95% each side."""
    rt = rh_spread.round_trip_bps(0.95)
    assert 191.0 < rt < 193.0, f"expected ~191.8 bps, got {rt:.2f}"


def test_no_coin_is_ever_cheaper_than_the_spread():
    """A hurdle below the round trip would let a losing trade through."""
    for sym in ("BTC", "DOGE", "SOL"):
        g = rh_spread.get(sym)
        assert rh_spread.hurdle_bps(sym) > g["round_trip_bps"], sym
'''
