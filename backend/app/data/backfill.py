"""Multi-year historical backfill.

The single biggest constraint on this project was that a live feed serves only a
few hundred recent candles, so every strategy was data-starved and every Model
Lab verdict was INSUFFICIENT_DATA. This module removes that constraint.

Coinbase Exchange's public candle endpoint accepts `start` and `end`, returning
at most 300 candles per call. Walking those windows backwards gives years of
history for free, with no API key.

What is actually available, per granularity
-------------------------------------------
    86400 (1 day)    ~10+ years for the majors, back to each coin's listing
     3600 (1 hour)   several years; the workhorse for swing and rotation work
      900 (15 min)   1-2 years typically
       60 (1 min)    only weeks. Do NOT plan on minute history from any public
                     venue -- it is collected going forward, not backfilled.

That last line matters for strategy design: the fast strategies can only ever be
validated on data collected from today onwards, while the longer-horizon ones can
be validated on years of history starting immediately. It is another reason the
swing and rotation ideas are the ones worth working on first.

Cost of storage (measured, not guessed -- see `storage_estimate()`)
------------------------------------------------------------------
A bar row in SQLite costs roughly 100-130 bytes including the primary-key index.
For 80 coins over 4 years that is about 340 MB at hourly and 1.3 GB at 15-minute
resolution. Both are unremarkable on a laptop. One-minute for 80 coins would run
about 5 GB per year going forward, which is where pruning starts to matter.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx

from app.config import get_settings
from app.core import db
from app.data.feeds import get_feed

MAX_CANDLES_PER_REQUEST = 300
RATE_LIMIT_SLEEP = 0.12          # ~8 req/s, under Coinbase's ~10 req/s public cap
MAX_RETRIES = 3

GRANULARITIES = {
    60: "1 minute", 300: "5 minutes", 900: "15 minutes",
    3600: "1 hour", 21600: "6 hours", 86400: "1 day",
}

_state: dict = {
    "running": False, "thread": None, "cancel": False,
    "job": None, "progress": {}, "log": [],
}
_lock = threading.Lock()


@dataclass
class JobProgress:
    granularity: int
    symbols_total: int
    symbols_done: int = 0
    current_symbol: str | None = None
    rows_written: int = 0
    requests_made: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at
        rate = self.symbols_done / elapsed if elapsed > 0 else 0
        remaining = self.symbols_total - self.symbols_done
        return {
            **self.__dict__,
            "elapsed_s": elapsed,
            "eta_s": (remaining / rate) if rate > 0 and remaining > 0 else None,
            "pct": (100.0 * self.symbols_done / self.symbols_total) if self.symbols_total else 0.0,
        }


def _client() -> httpx.Client:
    return httpx.Client(timeout=20.0, follow_redirects=True,
                        headers={"User-Agent": "tradecrypto/0.1 (personal research)"})


def fetch_window(client: httpx.Client, product: str, granularity: int,
                 start: int, end: int) -> list[tuple]:
    """One Coinbase candle window. Returns [time, low, high, open, close, volume]."""
    url = f"https://api.exchange.coinbase.com/products/{product}/candles"
    params = {"granularity": granularity,
              "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
              "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end))}
    for attempt in range(MAX_RETRIES):
        r = client.get(url, params=params)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited on {product} after {MAX_RETRIES} attempts")


def backfill_symbol(symbol: str, product: str, granularity: int, days: float,
                    progress: JobProgress | None = None,
                    should_cancel=lambda: False) -> int:
    """Walk backwards in 300-candle windows until `days` are covered or the
    exchange stops returning data (which is how we discover a coin's listing date).
    """
    now = int(time.time())
    oldest_wanted = now - int(days * 86400)
    window = granularity * MAX_CANDLES_PER_REQUEST
    end = now
    written = 0
    empty_streak = 0

    with _client() as client:
        while end > oldest_wanted and not should_cancel():
            start = max(end - window, oldest_wanted)
            try:
                rows = fetch_window(client, product, granularity, start, end)
                if progress:
                    progress.requests_made += 1
            except Exception as exc:
                if progress:
                    progress.errors.append(f"{symbol} {granularity}s: {type(exc).__name__}: {exc}")
                break

            if not rows:
                empty_streak += 1
                # Two empty windows in a row means we have reached the coin's
                # listing date; there is nothing older to fetch.
                if empty_streak >= 2:
                    break
                end = start
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            empty_streak = 0

            db.executemany(
                """INSERT OR IGNORE INTO bars(symbol, ts, granularity, open, high, low,
                                              close, volume, source)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(symbol, int(x[0]), granularity, float(x[3]), float(x[2]),
                  float(x[1]), float(x[4]), float(x[5]), "coinbase") for x in rows],
            )
            written += len(rows)
            if progress:
                progress.rows_written += len(rows)
            end = min(int(min(x[0] for x in rows)), start)
            time.sleep(RATE_LIMIT_SLEEP)
    return written


def _run_job(granularity: int, days: float, symbols: list[str] | None) -> None:
    feed = get_feed()
    try:
        products = feed.products()
    except Exception as exc:
        db.log_event("ERROR", "backfill", f"cannot list products: {exc}")
        with _lock:
            _state["running"] = False
        return

    rows = db.query("SELECT symbol, feed_product FROM universe WHERE active=1 ORDER BY symbol")
    targets = [(r["symbol"], r["feed_product"]) for r in rows]
    if symbols:
        want = {s.upper() for s in symbols}
        targets = [t for t in targets if t[0] in want]
    targets = [(s, p) for s, p in targets if p in products.values() or s in products]

    prog = JobProgress(granularity=granularity, symbols_total=len(targets))
    with _lock:
        _state["progress"] = prog
    db.log_event("INFO", "backfill",
                 f"starting {GRANULARITIES.get(granularity, granularity)} backfill of "
                 f"{len(targets)} symbols over {days:.0f} days")

    for sym, product in targets:
        if _state["cancel"]:
            break
        prog.current_symbol = sym
        try:
            backfill_symbol(sym, product, granularity, days, prog,
                            should_cancel=lambda: _state["cancel"])
        except Exception as exc:
            prog.errors.append(f"{sym}: {type(exc).__name__}: {exc}")
        prog.symbols_done += 1

    prog.finished_at = time.time()
    with _lock:
        _state["running"] = False
    db.log_event("INFO", "backfill",
                 f"finished: {prog.rows_written:,} rows in {prog.requests_made:,} requests, "
                 f"{len(prog.errors)} errors")


def start(granularity: int = 3600, days: float = 1460, symbols: list[str] | None = None) -> dict:
    with _lock:
        if _state["running"]:
            return {"started": False, "reason": "a backfill is already running",
                    "progress": status()}
        if granularity not in GRANULARITIES:
            return {"started": False, "reason": f"granularity must be one of {sorted(GRANULARITIES)}"}
        _state["running"] = True
        _state["cancel"] = False
        _state["job"] = {"granularity": granularity, "days": days, "symbols": symbols}
        t = threading.Thread(target=_run_job, args=(granularity, days, symbols),
                             daemon=True, name="tc-backfill")
        _state["thread"] = t
        t.start()
    return {"started": True, "job": _state["job"],
            "note": ("Runs in the background. Hourly over 4 years across the whole universe "
                     "is roughly 9,000 requests and takes about 15-25 minutes.")}


def cancel() -> dict:
    _state["cancel"] = True
    return {"cancelling": True}


def status() -> dict:
    prog = _state.get("progress")
    return {
        "running": _state["running"],
        "job": _state.get("job"),
        "progress": prog.to_dict() if isinstance(prog, JobProgress) else None,
    }


# ── coverage and storage ─────────────────────────────────────────────────────
def coverage() -> dict:
    """What history do we actually hold? This is what the date filters read."""
    rows = db.query(
        """SELECT symbol, granularity, COUNT(*) n, MIN(ts) first_ts, MAX(ts) last_ts
           FROM bars GROUP BY symbol, granularity ORDER BY granularity, symbol""")
    by_gran: dict[int, dict] = {}
    for r in rows:
        g = r["granularity"]
        b = by_gran.setdefault(g, {"granularity": g, "label": GRANULARITIES.get(g, str(g)),
                                   "symbols": 0, "rows": 0, "first_ts": None, "last_ts": None,
                                   "per_symbol": []})
        b["symbols"] += 1
        b["rows"] += r["n"]
        b["first_ts"] = r["first_ts"] if b["first_ts"] is None else min(b["first_ts"], r["first_ts"])
        b["last_ts"] = r["last_ts"] if b["last_ts"] is None else max(b["last_ts"], r["last_ts"])
        span_days = (r["last_ts"] - r["first_ts"]) / 86400 if r["last_ts"] else 0
        expected = span_days * 86400 / g if g else 0
        b["per_symbol"].append({
            "symbol": r["symbol"], "rows": r["n"],
            "first_ts": r["first_ts"], "last_ts": r["last_ts"],
            "span_days": span_days,
            "completeness_pct": (100.0 * r["n"] / expected) if expected > 0 else None,
        })
    out = list(by_gran.values())
    for b in out:
        b["span_days"] = (b["last_ts"] - b["first_ts"]) / 86400 if b["last_ts"] else 0
        b["per_symbol"].sort(key=lambda x: x["rows"], reverse=True)
    out.sort(key=lambda b: b["granularity"])
    return {"by_granularity": out,
            "total_rows": sum(b["rows"] for b in out),
            "database_bytes": _db_size()}


def _db_size() -> int:
    try:
        return get_settings().db_file.stat().st_size
    except OSError:
        return 0


def storage_estimate(n_symbols: int = 80) -> dict:
    """Measured bytes-per-row from this database, projected out.

    Uses the real file size and the real row count rather than a textbook number,
    so the projection reflects SQLite's actual overhead and indexes here.
    """
    total_rows = db.query_one("SELECT COUNT(*) c FROM bars")["c"] or 0
    size = _db_size()
    bytes_per_row = (size / total_rows) if total_rows > 500 else 120.0

    def proj(gran: int, years: float) -> dict:
        rows = int(years * 365 * 86400 / gran) * n_symbols
        return {"granularity": GRANULARITIES.get(gran, str(gran)), "years": years,
                "rows": rows, "megabytes": rows * bytes_per_row / 1e6}

    return {
        "measured_bytes_per_row": bytes_per_row,
        "current_rows": total_rows,
        "current_database_mb": size / 1e6,
        "assumed_symbols": n_symbols,
        "projections": [
            proj(86400, 4), proj(3600, 4), proj(900, 2), proj(900, 4),
            proj(60, 1), proj(60, 4),
        ],
        "recommendation": (
            "Daily and hourly for 4 years is a few hundred megabytes -- do it without "
            "thinking about it. 15-minute for 1-2 years is about a gigabyte, still fine. "
            "One-minute is the only one that gets expensive (roughly 5 GB per year across "
            "80 coins), it cannot be backfilled from public feeds anyway, and it is only "
            "needed for the fast strategies. Collect 1-minute going forward for the ~20 "
            "coins you actually trade, not all 80."
        ),
    }


def raw_bars(symbol: str, granularity: int = 3600,
             start_ts: float | None = None, end_ts: float | None = None,
             limit: int = 5000) -> dict:
    """The raw-data browser behind the date filter."""
    sql = "SELECT ts, open, high, low, close, volume, source FROM bars WHERE symbol=? AND granularity=?"
    params: list = [symbol.upper(), granularity]
    if start_ts:
        sql += " AND ts >= ?"; params.append(int(start_ts))
    if end_ts:
        sql += " AND ts <= ?"; params.append(int(end_ts))
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    return {"symbol": symbol.upper(), "granularity": granularity,
            "label": GRANULARITIES.get(granularity, str(granularity)),
            "n": len(rows), "rows": rows[::-1]}
