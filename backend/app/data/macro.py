"""Non-crypto context: equities, the dollar, gold, oil, volatility.

the operator's point is a good one — crypto does not trade in a vacuum, and the
relationship to risk assets and to the dollar is real enough to be worth a
factor. But it has to be MEASURED, because it is also unstable: BTC has been
described as a risk asset, an inflation hedge and a dollar hedge in the same
decade, and it cannot have been all three at once.

So this module does two things and refuses to do a third:

  it FETCHES daily bars for a handful of macro series (free, no key, Stooq)
  it MEASURES the rolling correlation of each to BTC, with a confidence interval
  it does NOT feed any of this into a strategy until a relationship is shown to
    be both significant and stable

An important limitation, stated up front: these series are DAILY and most are
closed at night and at weekends, while crypto trades continuously. That makes
them useful as a regime filter — "are risk assets bid this week" — and close to
useless for a 30-minute trade. Anyone claiming an intraday crypto signal from
daily S&P data is fooling themselves.

Correlation is computed on overlapping daily returns only, and reported with a
Fisher z confidence interval so a value of 0.2 on 40 observations is visibly not
the same claim as 0.2 on 400.
"""
from __future__ import annotations

import csv
import io
import math
import time

import httpx
import numpy as np
from scipy import stats as sps

from app.core import db

# Yahoo's chart JSON; free, no key.
#
# This used to read Stooq's CSV endpoint. As of 2026-09-13 Stooq answers every
# request with a JavaScript proof-of-work browser challenge instead of data, so
# all seven series failed every night and the job sat red permanently. That is
# not a transient outage and no retry schedule fixes it -- the endpoint is simply
# closed to programs now.
#
# Each symbol below was checked against the live API before switching, on
# 2026-09-13: ^GSPC 7656.98, ^NDX 29368.44, ^VIX 15.84, DX-Y.NYB 99.139,
# GC=F 4381.9, CL=F 102.4, ^TNX 4.975.
SERIES = [
    ("SPX", "^GSPC", "S&P 500", "broad US equity risk appetite"),
    ("NDX", "^NDX", "Nasdaq 100", "tech beta — historically the closest equity proxy for crypto"),
    ("VIX", "^VIX", "VIX", "equity volatility; spikes tend to coincide with crypto drawdowns"),
    ("DXY", "DX-Y.NYB", "US Dollar Index", "dollar strength — the most commonly claimed inverse relationship"),
    ("GOLD", "GC=F", "Gold", "the 'digital gold' comparison, testable rather than assumed"),
    ("OIL", "CL=F", "WTI Crude", "inflation and global growth proxy"),
    ("US10Y", "^TNX", "US 10-year yield", "the discount rate on every long-duration asset"),
]
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS macro_bars (
        series TEXT NOT NULL, date TEXT NOT NULL, ts REAL NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (series, date))""")


def fetch(timeout: float = 20.0) -> dict:
    """Pull daily history for each series. Failures are reported, never hidden."""
    ensure_schema()
    added, errors, got = 0, [], []
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; tradecrypto/0.1 personal research)"}) as c:
        for key, sym, label, _why in SERIES:
            try:
                r = c.get(f"{YAHOO_CHART}{sym}",
                          params={"range": "2y", "interval": "1d"})
                r.raise_for_status()
                payload = r.json()
                res = ((payload.get("chart") or {}).get("result") or [])
                if not res:
                    err = ((payload.get("chart") or {}).get("error") or {})
                    errors.append(f"{key}: no data for '{sym}'"
                                  + (f" ({err.get('description')})" if err else ""))
                    continue
                block = res[0]
                stamps = block.get("timestamp") or []
                qs = ((block.get("indicators") or {}).get("quote") or [{}])[0]
                op, hi, lo_, cl, vol = (qs.get("open") or [], qs.get("high") or [],
                                        qs.get("low") or [], qs.get("close") or [],
                                        qs.get("volume") or [])
                batch = []
                for i, t_ in enumerate(stamps):
                    try:
                        if cl[i] is None:          # holidays come back as nulls
                            continue
                        d = time.strftime("%Y-%m-%d", time.gmtime(float(t_)))
                        batch.append((key, d, float(t_), _f(op[i]), _f(hi[i]),
                                      _f(lo_[i]), _f(cl[i]),
                                      _f(vol[i]) if i < len(vol) else None))
                    except Exception:
                        continue
                if batch:
                    db.executemany(
                        """INSERT OR REPLACE INTO macro_bars(series, date, ts, open, high, low, close, volume)
                           VALUES (?,?,?,?,?,?,?,?)""", batch)
                    added += len(batch)
                    got.append(f"{key} ({len(batch)} days)")
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
    db.log_event("INFO", "macro", f"macro fetch: {len(got)} series, {added} rows, {len(errors)} errors")
    return {"series_loaded": got, "rows": added, "errors": errors,
            "source": "Yahoo chart API (free, keyless)",
            "note": "Yahoo daily bars. Stooq was dropped on 2026-09-13: it now answers "
                    "programs with a JavaScript proof-of-work challenge instead of data. "
                    "A failed series is reported, never silently skipped."}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _daily_returns(series: str, days: int) -> dict[str, float]:
    rows = db.query(
        "SELECT date, close FROM macro_bars WHERE series=? ORDER BY date DESC LIMIT ?",
        (series, days + 1))
    rows = rows[::-1]
    out = {}
    for a, b in zip(rows, rows[1:]):
        if a["close"] and b["close"] and a["close"] > 0:
            out[b["date"]] = b["close"] / a["close"] - 1.0
    return out


def _btc_daily_returns(days: int) -> dict[str, float]:
    rows = db.query(
        """SELECT ts, close FROM bars WHERE symbol='BTC' AND granularity=86400
           ORDER BY ts DESC LIMIT ?""", (days + 1,))
    if len(rows) < 10:
        # fall back to hourly closes sampled at 00:00 UTC
        rows = db.query(
            """SELECT ts, close FROM bars WHERE symbol='BTC' AND granularity=3600
               ORDER BY ts DESC LIMIT ?""", (days * 24 + 24,))
        seen, daily = set(), []
        for r in rows:
            d = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
            if d not in seen:
                seen.add(d)
                daily.append(r)
        rows = daily
    rows = rows[::-1]
    out = {}
    for a, b in zip(rows, rows[1:]):
        if a["close"] and b["close"] and a["close"] > 0:
            out[time.strftime("%Y-%m-%d", time.gmtime(b["ts"]))] = b["close"] / a["close"] - 1.0
    return out


def correlations(days: int = 365) -> dict:
    """Correlation of each macro series with BTC on overlapping daily returns."""
    ensure_schema()
    btc = _btc_daily_returns(days)
    if len(btc) < 30:
        return {"available": False,
                "note": f"only {len(btc)} daily BTC returns — backfill daily bars first"}

    rows = []
    for key, _stooq, label, why in SERIES:
        mac = _daily_returns(key, days)
        common = sorted(set(btc) & set(mac))
        n = len(common)
        if n < 30:
            rows.append({"series": key, "label": label, "n": n, "rho": None,
                         "sufficient": False, "why_it_might_matter": why,
                         "note": "not enough overlapping days"})
            continue
        x = np.array([btc[d] for d in common])
        y = np.array([mac[d] for d in common])
        rho = float(np.corrcoef(x, y)[0, 1])
        # Fisher z interval — a correlation without one is a number, not a finding
        z = 0.5 * math.log((1 + rho) / (1 - rho)) if abs(rho) < 0.999 else 0.0
        se = 1 / math.sqrt(max(n - 3, 1))
        lo, hi = math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)
        # stability: split-half comparison
        half = n // 2
        r1 = float(np.corrcoef(x[:half], y[:half])[0, 1]) if half > 15 else float("nan")
        r2 = float(np.corrcoef(x[half:], y[half:])[0, 1]) if n - half > 15 else float("nan")
        stable = bool(np.isfinite(r1) and np.isfinite(r2) and np.sign(r1) == np.sign(r2)
                      and abs(r1 - r2) < 0.25)
        rows.append({
            "series": key, "label": label, "n": n, "rho": rho, "ci95": [lo, hi],
            "significant": bool(lo > 0 or hi < 0),
            "first_half_rho": r1, "second_half_rho": r2, "stable": stable,
            "sufficient": True, "why_it_might_matter": why,
            "verdict": (
                "significant and stable — worth using as a regime filter" if (lo > 0 or hi < 0) and stable
                else "significant but unstable across the sample — do not build on it"
                if (lo > 0 or hi < 0) else "indistinguishable from zero"),
        })
    rows.sort(key=lambda r: -(abs(r["rho"]) if r["rho"] is not None else -1))
    usable = [r for r in rows if r.get("significant") and r.get("stable")]
    return {
        "available": True, "days": days, "btc_days": len(btc), "rows": rows,
        "usable": [r["series"] for r in usable],
        "caveat": (
            "Daily series against a 24/7 market. Useful as a weekly regime filter, "
            "close to useless for a 30-minute trade — equity markets are shut for "
            "most of the hours crypto trades. Nothing here feeds a strategy until a "
            "relationship is both significant and stable across halves of the sample."
        ),
    }


def latest() -> dict:
    ensure_schema()
    rows = []
    for key, _s, label, why in SERIES:
        r = db.query_one(
            "SELECT date, close FROM macro_bars WHERE series=? ORDER BY date DESC LIMIT 1", (key,))
        prev = db.query(
            "SELECT close FROM macro_bars WHERE series=? ORDER BY date DESC LIMIT 6", (key,))
        chg = None
        if r and len(prev) > 1 and prev[-1]["close"]:
            chg = (r["close"] / prev[-1]["close"] - 1) * 100
        rows.append({"series": key, "label": label, "why": why,
                     "date": r["date"] if r else None,
                     "close": r["close"] if r else None,
                     "change_5d_pct": chg})
    return {"rows": rows}
