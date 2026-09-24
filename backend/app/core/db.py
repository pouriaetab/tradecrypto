"""SQLite persistence.

Everything the engine sees, decides, and does is written here. The dashboard
reads only from this database, so what you see on screen is literally the audit
trail -- there is no second, hidden state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.config import get_settings
from app.core import dbguard, dbrecover

_LOCK = threading.Lock()

# Who holds the one database lock, since when, doing what. 2026-09-21 10:10:
# /mode (one row) took 27 s and /health/report (no database at all) took 24 s
# while the tick profile showed a 365-second tick. Every reading we had said
# "slow"; none said WHO. This is the who. Read through GET /system/threads.
LOCK_HOLDER: dict = {"thread": None, "since": 0.0, "what": None}
LOCK_WAITS: dict = {"n": 0, "max_wait_s": 0.0, "total_wait_s": 0.0}


def _acquire(what: str) -> float:
    t0 = time.perf_counter()
    _LOCK.acquire()
    waited = time.perf_counter() - t0
    LOCK_WAITS["n"] += 1
    LOCK_WAITS["total_wait_s"] += waited
    if waited > LOCK_WAITS["max_wait_s"]:
        LOCK_WAITS["max_wait_s"] = waited
    LOCK_HOLDER.update(thread=threading.current_thread().name, since=time.time(), what=what[:160])
    return waited


def _release() -> None:
    LOCK_HOLDER.update(thread=None, since=0.0, what=None)
    _LOCK.release()


@contextmanager
def _held(what: str):
    _acquire(what)
    try:
        yield
    finally:
        _release()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── market data we collect ourselves (Robinhood keeps no history for us) ────
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    ts          INTEGER NOT NULL,          -- unix seconds, bar OPEN time
    granularity INTEGER NOT NULL,          -- seconds
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    source      TEXT NOT NULL,
    PRIMARY KEY (symbol, granularity, ts, source)
);
CREATE INDEX IF NOT EXISTS ix_bars_sym_ts ON bars(symbol, ts);

CREATE TABLE IF NOT EXISTS quotes (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    symbol TEXT NOT NULL,
    bid REAL, ask REAL, mid REAL, last REAL,
    spread_bps REAL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quotes_sym_ts ON quotes(symbol, ts);

CREATE TABLE IF NOT EXISTS universe (
    symbol       TEXT PRIMARY KEY,         -- canonical, e.g. DOGE
    feed_product TEXT NOT NULL,            -- e.g. DOGE-USD on coinbase
    feed_source  TEXT NOT NULL,
    rh_confirmed INTEGER NOT NULL DEFAULT 0,  -- 1 only once the RH MCP confirmed it
    active       INTEGER NOT NULL DEFAULT 1,
    min_notional REAL,
    note         TEXT,
    updated_at   REAL NOT NULL
);

-- ── decisions ───────────────────────────────────────────────────────────────
-- Key/value scratch the app relies on from its very first tick. It used to be
-- created by whichever module happened to call ensure_schema() first, which
-- meant a BRAND NEW database had no app_state and sizing.share() raised
-- "no such table" before a single trade existed. A table the core depends on
-- belongs in the core schema (2026-09-24, found by running the handoff bundle
-- as a fresh install).
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_ts REAL
);

-- Created lazily by app/research/daily_report.py, which means it does not exist
-- until that job has run once. The DATA-INTEGRITY CHECK for it does not wait:
-- on a fresh install `reports_carry_a_shape` raised "no such table:
-- daily_reports", so the first thing a new user saw on the Data page was their
-- own install reporting itself broken. Same class as app_state above: a table
-- the app needs belongs in the schema, not in whichever module happens to
-- touch it first (webapp_blueprint CHECKLIST 5.53).
CREATE TABLE IF NOT EXISTS daily_reports (
    day TEXT PRIMARY KEY,
    built_at REAL NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                    -- buy | sell | flat
    raw_score REAL,
    expected_edge_bps REAL,                -- model's forecast, BEFORE costs
    edge_ci_low_bps REAL, edge_ci_high_bps REAL,
    cost_hurdle_bps REAL,                  -- what a round trip is expected to cost
    decision TEXT NOT NULL,                -- taken | rejected
    reject_reason TEXT,
    features_json TEXT,
    sample_size INTEGER
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

-- ── orders and fills ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT UNIQUE NOT NULL,
    signal_id INTEGER REFERENCES signals(id),
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    intent TEXT NOT NULL,                  -- open | close
    qty REAL NOT NULL,
    notional_usd REAL NOT NULL,
    mode TEXT NOT NULL,                    -- paper | advisory | mcp
    status TEXT NOT NULL,                  -- pending | submitted | filled | cancelled | rejected
    ts_decided REAL NOT NULL,
    ts_submitted REAL,
    ts_filled REAL,
    mid_at_decision REAL,
    mid_at_submit REAL,
    fill_price REAL,
    fee_usd REAL DEFAULT 0,
    broker_order_id TEXT,
    reject_reason TEXT,
    ticket_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_ts ON orders(ts_decided);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    mode TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_px REAL NOT NULL,
    exit_px REAL NOT NULL,
    ts_open REAL NOT NULL,
    ts_close REAL NOT NULL,
    holding_s REAL NOT NULL,
    gross_pnl_usd REAL NOT NULL,
    cost_usd REAL NOT NULL,
    net_pnl_usd REAL NOT NULL,
    predicted_edge_bps REAL,
    realised_edge_bps REAL,
    attribution_json TEXT,
    open_order_id INTEGER REFERENCES orders(id),
    close_order_id INTEGER REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS ix_trades_close ON trades(ts_close);

-- ── the thing everything else depends on: what execution actually costs ─────
CREATE TABLE IF NOT EXISTS cost_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    notional_usd REAL NOT NULL,
    mid_at_decision REAL,
    mid_at_submit REAL,
    fill_px REAL NOT NULL,
    quoted_spread_bps REAL,
    half_spread_bps REAL,                  -- (fill - mid_submit)/mid_submit, signed by side
    shortfall_bps REAL,                    -- Perold implementation shortfall vs decision mid
    latency_ms REAL,
    realised_vol_bps REAL,                 -- regime tag
    mode TEXT NOT NULL,
    source TEXT NOT NULL                   -- measured | prior | manual_report
);
CREATE INDEX IF NOT EXISTS ix_cost_sym ON cost_observations(symbol, ts);

-- ── portfolio ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_px REAL NOT NULL,
    opened_ts REAL NOT NULL,
    stop_px REAL, target_px REAL, max_hold_s REAL,
    trail_bps REAL, peak_px REAL,
    PRIMARY KEY (symbol, mode, strategy)
);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts REAL NOT NULL,
    mode TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    realised_pnl REAL NOT NULL,
    unrealised_pnl REAL NOT NULL,
    PRIMARY KEY (ts, mode)
);

-- ── transparency + learning ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_cards (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS strategy_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,                  -- research | paper | live | halted
    allocation_frac REAL NOT NULL,
    posterior_json TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_stratstate ON strategy_state(strategy, ts);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                    -- backtest | walkforward | costfit | selftest
    label TEXT,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    verdict TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
"""


def _connect() -> sqlite3.Connection:
    s = get_settings()
    s.db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(s.db_file, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL is not enough on its own: with the default synchronous=FULL, SQLite
    # fsyncs on EVERY commit. Measured on a sibling project (webapp_blueprint
    # CHECKLIST 4.20) at 4.99 ms median and 34.7 ms worst per commit on a much
    # smaller file than ours -- and this desk commits on every tick, on the same
    # loop that has to notice a stop being hit.
    #
    # synchronous=NORMAL under WAL is the documented pairing: a process crash
    # still cannot corrupt the file, and the only exposure is losing the last
    # transactions to a power cut. We can afford exactly that, because every
    # event that changes what the desk owns or owes is ALSO fsync'd to the
    # append-only journal (core/journal.py) -- which is the copy that survives.
    #
    # Per-connection, not stored in the file, so it belongs here rather than in
    # SCHEMA: every connection this factory hands out gets it.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_conn: sqlite3.Connection | None = None


# Columns added after the table was first created. Idempotent; order irrelevant.
_ADD_COLUMNS = (
    # EVERY GATE THAT JUDGED THIS SIGNAL, with its limit and the actual value.
    # 2026-09-23, the operator: "include any measurement that contributed to
    # each decision behind something becoming signal and then signal becoming an
    # order." `reject_reason` only ever held the FIRST failure as a sentence; the
    # other seventeen checks, their thresholds and their readings were computed
    # on every signal and thrown away.
    # The moment a position's odds of reaching its target first fell below the
    # bar. The target comes down from HERE -- not from a fixed hour. Stored
    # rather than recomputed because it is a one-way door: a trade that has
    # broken once has shown its character.
    ("positions", "sluggish_since", "REAL"),
    ("positions", "sluggish_odds", "REAL"),
    ("signals", "checks_json", "TEXT"),
    # How many candidates that strategy was choosing between in that bar, and
    # where this one placed. Stored rather than recomputed because the race is
    # only reconstructible while every signal of that bar survives.
    ("signals", "field_size", "INTEGER"),
    ("signals", "field_rank", "INTEGER"),
    ("positions", "trail_bps", "REAL"),
    ("positions", "peak_px", "REAL"),
    # Which order opened this position, so the closed trade can be traced back to
    # the signal and the features that caused it. Without it, trades.open_order_id
    # was NULL on every row and no live outcome could ever be joined to its
    # inputs — which makes learning from real trading impossible in principle.
    ("positions", "open_order_id", "INTEGER"),
    # How many times this position's stop has actually been raised. Without it
    # "trailing" is a claim; with it, it is a count you can read off the Risk
    # tab. Every row starts at 0, which on 2026-09-19 was also the true answer
    # for every position that had ever existed.
    ("positions", "stop_moves", "INTEGER DEFAULT 0"),
)


# What the boot integrity check found, for the status page and the daily report.
LAST_RECOVERY: dbrecover.Report | None = None

# Anything that is not the trading app -- a script, an experiment, a look from
# another machine -- opens the live database with TC_DB_READONLY=1 and cannot
# corrupt it. There is no read-write path from outside the app that does not go
# past dbguard.claim() first.
def readonly_mode() -> bool:
    import os
    return os.environ.get("TC_DB_READONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def get_conn() -> sqlite3.Connection:
    global _conn, LAST_RECOVERY
    with _LOCK:
        if _conn is None:
            s = get_settings()
            s.db_file.parent.mkdir(parents=True, exist_ok=True)

            if readonly_mode():
                # immutable=1, not just mode=ro. A WAL database is read through
                # a shared-memory index (-shm) that every reader MAPS AND WRITES
                # TO, so `mode=ro` is not a promise to leave the file alone -- it
                # only promises not to change the main file. Across the bridge
                # mount that mapping is not coherent with the macOS app's, which
                # is the same class of interference that destroyed the database
                # on 2026-09-18.
                #
                # immutable=1 tells SQLite the file cannot change, so it skips
                # WAL and the -shm entirely: pure reads, nothing mapped, nothing
                # written. The cost is that a concurrently-updated file may read
                # stale or raise -- acceptable for an audit, and it cannot touch
                # the running desk.
                _conn = sqlite3.connect(f"file:{s.db_file}?mode=ro&immutable=1",
                                        uri=True, timeout=30, check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                return _conn

            # Claim FIRST. It writes one small JSON file and opens no database,
            # so it is safe even against a file we have not inspected yet -- and
            # the inspection itself opens the database read-write, which must
            # never happen while another machine holds it.
            dbguard.claim(s.db_file, role="tradecrypto-app")

            # Repair before the schema touches it, so a corrupt file is a
            # five-second recovery instead of a five-hour crash loop.
            LAST_RECOVERY = dbrecover.preflight(s.db_file)
            print(f"[db] {LAST_RECOVERY.headline()}", flush=True)
            for n in LAST_RECOVERY.notes:
                print(f"[db]   {n}", flush=True)
            if not LAST_RECOVERY.healthy:
                raise sqlite3.DatabaseError(LAST_RECOVERY.headline())

            _conn = _connect()
            _conn.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS never adds a column to a table that
            # already exists, so a schema change has to be applied separately or
            # it silently only reaches brand-new databases.
            for table, col, decl in _ADD_COLUMNS:
                try:
                    _conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                except sqlite3.OperationalError:
                    pass            # already present
            _conn.commit()
        return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    with _held("tx"):
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    conn = get_conn()
    with _held(sql):
        cur = conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def stream(sql: str, params: Iterable[Any] = (), chunk: int = 50_000):
    """Yield result rows in chunks, as plain tuples.

    query() materialises the entire result AND turns every row into a dict.
    Filling a price matrix that way means millions of interpreter objects
    allocated before a single number reaches numpy -- hundreds of megabytes to
    produce a hundred. A caller writing into an array wants the rows, not
    dictionaries, and wants them a chunk at a time.
    """
    conn = get_conn()
    with _held("stream: " + sql):
        cur = conn.execute(sql, tuple(params))
        cur.row_factory = None          # plain tuples, not sqlite3.Row
    try:
        while True:
            with _held("stream chunk: " + sql):
                batch = cur.fetchmany(chunk)
            if not batch:
                return
            yield batch
    finally:
        try:
            cur.close()
        except Exception:
            pass


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


# Answers that are informational and expensive share one copy for a while.
#
# 2026-09-21 10:10: /system/status and /setup are polled by every open tab
# every few seconds, and each call ran COUNT(*) over the 3-million-row bars
# table and MIN/MAX(ts) over it (no index on ts alone: a full scan, 1.1-1.7 s
# measured under the lock). Ninety seconds after a boot the lock had been
# acquired 3,577 times with 354 s of cumulative waiting -- four callers deep
# on a one-lane road. The engine's tick waited in the same queue: one took
# 365 s, and /mode (one row) took 27 s. Nobody needs a bar count that is
# less than two minutes old.
_MEMO: dict[str, tuple[float, Any]] = {}
_MEMO_LOCK = threading.Lock()


def query_one_cached(sql: str, params: Iterable[Any] = (), ttl_s: float = 120.0) -> dict | None:
    """query_one, but callers within `ttl_s` of each other share one execution."""
    key = f"{sql}|{tuple(params)!r}"
    now = time.time()
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
        if hit and (now - hit[0]) < ttl_s:
            return hit[1]
    row = query_one(sql, params)
    with _MEMO_LOCK:
        _MEMO[key] = (time.time(), row)     # aged from when the answer arrived
    return row


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with tx() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid or 0


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with tx() as conn:
        conn.executemany(sql, [tuple(p) for p in seq])


def log_event(level: str, category: str, message: str, payload: dict | None = None) -> None:
    """Write one event. Serialising the payload must never take down the caller.

    `universe_review` died daily on "Object of type bytes is not JSON
    serializable" -- a leftover from the database salvage, where some values came
    back as bytes. The job did its real work and then failed at the logging step,
    so the failure looked like a broken job rather than a broken log line.
    """
    blob = None
    if payload:
        try:
            blob = json.dumps(payload, default=_json_fallback)
        except Exception as exc:                      # never lose the event itself
            blob = json.dumps({"payload_unserializable": f"{type(exc).__name__}: {exc}"})
    execute(
        "INSERT INTO events(ts, level, category, message, payload_json) VALUES (?,?,?,?,?)",
        (time.time(), level.upper(), category, message, blob),
    )


def _json_fallback(o: object) -> str:
    if isinstance(o, (bytes, bytearray)):
        try:
            return o.decode("utf-8", "replace")
        except Exception:
            return repr(o)
    return str(o)


def init_db() -> Path:
    get_conn()
    return get_settings().db_file
