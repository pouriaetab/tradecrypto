"""Tests may read the live database. They may never touch it.

Several tests here deliberately audit the *real* database -- that is the point
of the invariant suite, and pointing it at a fixture would make it check
nothing. But "the tests read production" and "the tests can write production"
are one env var apart, and on 2026-09-18 the second one destroyed the file:
running this suite from a machine that reaches the database over a mount opens
it read-write, and SQLite's locking does not cross that mount.

Read-only alone is not enough either. A WAL database is read through a shared
-shm index that every reader maps and writes to, so `mode=ro` still touches
files the running app depends on. TC_DB_READONLY therefore opens the live file
with `immutable=1`, which skips WAL and the -shm completely: nothing is mapped
and nothing is written, so the suite cannot reach the desk at all.

So the whole suite runs read-only by default. A test that needs to write asks
for the `writable_db` fixture and gets a private copy in a temp directory.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Set before anything imports app.config, which caches its settings on first use.
os.environ.setdefault("TC_DB_READONLY", "1")

# And the declared stake. Position caps and the daily-loss limit are both
# derived from TC_ACCOUNT_EQUITY, so with it read from the operator's own .env
# the SUITE'S RESULTS MOVE WHEN THE OPERATOR CHANGES THEIR STAKE. Two tests
# passed at $2,000 and failed at $500 -- which meant the first thing a new user
# did (set their own stake) broke tests that had nothing to do with them, and
# made a correct install look broken. Pin it, for the whole suite.
os.environ["TC_ACCOUNT_EQUITY"] = "2000"

# And the vault. It lives OUTSIDE the project by design, which means a test that
# forgets to point it somewhere safe writes the operator's real one -- and the
# vault is append-only, so there is no taking it back out. Forced here, for the
# whole suite, rather than trusted to each test (webapp_blueprint CHECKLIST 2.13).
_TEST_VAULT = Path(tempfile.mkdtemp(prefix="tc-test-vault-"))
os.environ["TC_VAULT_DIR"] = str(_TEST_VAULT)

# And the kill switch. It is a file in the project's data/ directory, and the
# risk guard writes it whenever the day's realised loss crosses the cap -- in
# WHATEVER database it is looking at. On 2026-09-21 08:37 a ceiling test set
# up a $150 loss in its private temp database, called pre_trade_check, and
# engaged the operator's real kill switch through the mount. The desk opened
# nothing for the rest of the morning on a loss that never happened, and the
# CRITICAL event went into the temp database, so the live log never said why.
# Forced here, for the whole suite; the session guard below refuses to run if
# the switch still resolves inside the project.
os.environ["TC_KILL_SWITCH_FILE"] = str(_TEST_VAULT / "KILL_SWITCH")


@pytest.fixture(scope="session", autouse=True)
def _remove_the_test_vault():
    """Take it away again. A per-run directory that is never removed is a slow
    disk leak, and a full disk shows up as a mysterious test failure rather than
    as "you are out of space"."""
    yield
    shutil.rmtree(_TEST_VAULT, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _never_write_production():
    """Fail loudly rather than quietly writing the desk's database."""
    if os.environ.get("TC_DB_READONLY", "").lower() not in {"1", "true", "yes", "on"}:
        pytest.exit("TC_DB_READONLY is off — refusing to run the suite against a "
                    "writable live database.", returncode=2)
    from app.config import PROJECT_ROOT, get_settings
    ks = get_settings().kill_switch_file
    if PROJECT_ROOT.resolve() in ks.resolve().parents:
        pytest.exit(f"the kill switch resolves to {ks}, inside the project — a test "
                    "that trips it would stop the live desk. Refusing to run.", returncode=2)
    yield


def has_table(name: str) -> bool:
    """Is `name` in the database the suite is reading?

    Off the Mac the suite reads a data/ledger/ snapshot (gate.sh sets
    TC_DB_PATH), which carries every irreplaceable table and deliberately not
    `bars` or `quotes` -- 470 MB of market data that is fetched again on
    demand. A test that needs those tables skips there, with the reason, and
    runs in full on the Mac. Skipped is the truth; an error is not.
    """
    from app.core import db
    try:
        return db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)) is not None
    except Exception:
        return False


def skip_without_history(min_trades: int = 1) -> None:
    """Skip a test that can only say something about a desk that has traded.

    A FRESH INSTALL HAS NO TRADES. Sixteen invariant tests -- "every trade links
    to its order", "P&L decomposes", "no slot cap is binding" -- passed
    vacuously or failed outright on an empty database, and the first thing a new
    user sees should not be sixteen red lines describing a desk they have not
    run yet. These tests check the HISTORY of a running desk; with no history
    there is nothing to check, and skipping says that out loud.
    """
    from app.core import db
    try:
        n = db.query_one("SELECT COUNT(*) c FROM trades WHERE ts_close IS NOT NULL")["c"]
    except Exception:
        n = 0
    if n < min_trades:
        pytest.skip(f"this checks the history of a desk that has traded; this "
                    f"database has {n} closed trade(s). Nothing to check yet.")


def skip_without(*tables: str) -> None:
    missing = [t for t in tables if not has_table(t)]
    if missing:
        pytest.skip(f"needs the live {', '.join(missing)} table(s); this database "
                    f"is a ledger snapshot — run on the Mac for this one")


@pytest.fixture(autouse=True)
def _kill_switch_starts_released():
    """Each test begins and ends with the (temp) kill switch off, so a test that
    trips it cannot make the next one's orders 'blocked' for no reason of its own."""
    ks = Path(os.environ["TC_KILL_SWITCH_FILE"])
    ks.unlink(missing_ok=True)
    yield
    ks.unlink(missing_ok=True)


@pytest.fixture
def writable_db(tmp_path, monkeypatch):
    """A private, empty database a test may do anything to.

    Built from the schema rather than copied: the live file is 440 MB and
    duplicating it per test would be slower than the entire suite.
    """
    from app.config import get_settings
    dest = tmp_path / "test.sqlite"
    monkeypatch.setenv("TC_DB_PATH", str(dest))
    monkeypatch.delenv("TC_DB_READONLY", raising=False)
    get_settings.cache_clear()
    from app.core import db as _db
    monkeypatch.setattr(_db, "_conn", None, raising=False)
    yield dest
    get_settings.cache_clear()
    monkeypatch.setattr(_db, "_conn", None, raising=False)
