"""A brand-new database must work on its first tick (2026-09-24).

Found by running the handoff bundle as a fresh install: `app_state` was created
by whichever module happened to call ensure_schema() first, so on a database
with no history `sizing.share()` raised "no such table: app_state" before a
single trade existed. A table the core depends on belongs in the CORE schema,
not in whichever module wins the race.

These tests read the schema that a new database is built from, rather than
building one -- the suite pins TC_DB_PATH and TC_DB_READONLY for safety, and a
test that fights that harness is testing the harness.
"""
import sqlite3

CORE_TABLES = ("trades", "orders", "positions", "signals", "app_state",
               "equity_curve", "events")


def test_the_base_schema_creates_every_table_the_core_needs():
    from app.core.db import SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [t for t in CORE_TABLES if t not in have]
    assert not missing, (
        f"a database built from the base schema is missing {missing} -- the first "
        f"tick of a fresh install will raise 'no such table'")


def test_app_state_is_in_the_core_schema_not_only_in_a_module():
    """It is also created by app.core.mode.ensure_schema(). That is fine as a
    belt, but the core must not DEPEND on that module having been imported."""
    from app.core.db import SCHEMA
    assert "CREATE TABLE IF NOT EXISTS app_state" in SCHEMA
