"""Every external API this app calls, and the RAW rows each one actually returns.

Asked for repeatedly and never built until now: "I like to see the data
examples/samples we are getting from each data source api ... click on a dataset
and see the raw data in form of table column and rows."

The point is not a description of each API. It is the bytes. Every sample below
is fetched LIVE when you ask for it, flattened into columns and rows exactly as
the provider sent them, with nothing renamed, rounded or filled in. If a field is
missing from the provider it is missing here; if the provider is down you see the
error it returned, not a blank table.

There is a second half: `local()` exposes the tables this app has WRITTEN from
those feeds, so you can compare what arrived against what was stored, and
`query()` runs read-only SQL against them.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.core import db

UA = {"User-Agent": "Mozilla/5.0 (compatible; tradecrypto/0.1 personal research)"}
TIMEOUT = 15.0


def catalog() -> list[dict]:
    """Every source, what it is for, and whether it needs a key."""
    return [
        {"key": "coinbase_products", "provider": "Coinbase Exchange",
         "url": "https://api.exchange.coinbase.com/products",
         "auth": "none", "used_for": "which pairs exist and how we name them",
         "feeds": "universe.feed_product"},
        {"key": "coinbase_ticker", "provider": "Coinbase Exchange",
         "url": "https://api.exchange.coinbase.com/products/{product}/ticker",
         "auth": "none", "used_for": "the live mid used for every paper fill",
         "feeds": "orders.mid_at_decision, equity_curve"},
        {"key": "coinbase_candles", "provider": "Coinbase Exchange",
         "url": "https://api.exchange.coinbase.com/products/{product}/candles",
         "auth": "none", "used_for": "every bar every strategy reads",
         "feeds": "bars"},
        {"key": "coinbase_stats", "provider": "Coinbase Exchange",
         "url": "https://api.exchange.coinbase.com/products/{product}/stats",
         "auth": "none", "used_for": "24h open/high/low/volume",
         "feeds": "movers (historical; now computed from bars)"},
        {"key": "robinhood_pairs", "provider": "Robinhood Crypto",
         "url": "https://trading.robinhood.com/api/v1/crypto/trading/trading_pairs/",
         "auth": "API key + Ed25519 signature",
         "used_for": "which coins are actually tradable (is_api_tradable)",
         "feeds": "universe.rh_confirmed"},
        {"key": "robinhood_bid_ask", "provider": "Robinhood Crypto",
         "url": "https://trading.robinhood.com/api/v1/crypto/marketdata/best_bid_ask/",
         "auth": "API key + Ed25519 signature",
         "used_for": "the spread, per coin — the largest term in every result",
         "feeds": "rh_spreads"},
        {"key": "yahoo_macro", "provider": "Yahoo Finance",
         "url": "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
         "auth": "none", "used_for": "SPX, NDX, VIX, DXY, gold, oil, 10y — display context only",
         "feeds": "macro_bars"},
        {"key": "news_rss", "provider": "CoinDesk / Cointelegraph / The Block",
         "url": "RSS", "auth": "none",
         "used_for": "hack, delisting and regulatory flags",
         "feeds": "news"},
    ]


def _rows_from(records: list[dict], limit: int) -> dict:
    """Turn provider records into columns + rows with nothing renamed."""
    recs = records[:limit]
    cols: list[str] = []
    for r in recs:
        for k in r:
            if k not in cols:
                cols.append(k)
    return {"columns": cols,
            "rows": [[r.get(c) for c in cols] for r in recs],
            "row_count": len(recs)}


def sample(key: str, product: str | None = None, limit: int = 25) -> dict:
    """Fetch one source live and return its raw rows."""
    t0 = time.time()
    meta = next((c for c in catalog() if c["key"] == key), None)
    if not meta:
        return {"error": f"unknown source {key!r}"}
    product = product or "BTC-USD"
    out: dict[str, Any] = {"key": key, "provider": meta["provider"],
                           "used_for": meta["used_for"], "fetched_at": time.time()}
    try:
        if key.startswith("coinbase_"):
            base = "https://api.exchange.coinbase.com"
            with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
                if key == "coinbase_products":
                    r = c.get(f"{base}/products"); r.raise_for_status()
                    data = [x for x in r.json() if str(x.get("id", "")).endswith("-USD")]
                    out["request_url"] = f"{base}/products"
                    out.update(_rows_from(data, limit))
                elif key == "coinbase_ticker":
                    url = f"{base}/products/{product}/ticker"
                    r = c.get(url); r.raise_for_status()
                    out["request_url"] = url
                    out.update(_rows_from([r.json()], limit))
                elif key == "coinbase_stats":
                    url = f"{base}/products/{product}/stats"
                    r = c.get(url); r.raise_for_status()
                    out["request_url"] = url
                    out.update(_rows_from([r.json()], limit))
                else:  # candles: the provider returns bare arrays, not objects
                    url = f"{base}/products/{product}/candles"
                    r = c.get(url, params={"granularity": 3600}); r.raise_for_status()
                    raw = r.json()[:limit]
                    out["request_url"] = url + "?granularity=3600"
                    out["columns"] = ["time", "low", "high", "open", "close", "volume"]
                    out["rows"] = [list(x) for x in raw]
                    out["row_count"] = len(raw)
                    out["note"] = ("Coinbase returns positional arrays here, not named "
                                   "fields. The column names are Coinbase's documented "
                                   "order — nothing in the response labels them.")
        elif key == "yahoo_macro":
            sym = product if product and product != "BTC-USD" else "^GSPC"
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            with httpx.Client(timeout=TIMEOUT, headers=UA) as c:
                r = c.get(url, params={"range": "1mo", "interval": "1d"})
                r.raise_for_status()
                res = ((r.json().get("chart") or {}).get("result") or [])
            out["request_url"] = url + "?range=1mo&interval=1d"
            if not res:
                out["error"] = "no result block returned"
            else:
                b = res[0]
                q = ((b.get("indicators") or {}).get("quote") or [{}])[0]
                stamps = b.get("timestamp") or []
                recs = []
                for i, t_ in enumerate(stamps[-limit:]):
                    j = len(stamps) - min(limit, len(stamps)) + i
                    recs.append({"timestamp": t_,
                                 "date": time.strftime("%Y-%m-%d", time.gmtime(float(t_))),
                                 "open": (q.get("open") or [None])[j] if q.get("open") else None,
                                 "high": (q.get("high") or [None])[j] if q.get("high") else None,
                                 "low": (q.get("low") or [None])[j] if q.get("low") else None,
                                 "close": (q.get("close") or [None])[j] if q.get("close") else None,
                                 "volume": (q.get("volume") or [None])[j] if q.get("volume") else None})
                out.update(_rows_from(recs, limit))
                out["meta_from_provider"] = b.get("meta")
        elif key.startswith("robinhood_"):
            # no broker integration in this build — the order-placing and account code was removed when this repository was published.
            out["error"] = "no broker integration in this build — the order-placing and account code was removed when this repository was published"
            return out
        elif False:
            c = None
            if key == "robinhood_pairs":
                data = c.trading_pairs()
                out["request_url"] = meta["url"]
                out.update(_rows_from(list(data), limit))
            else:
                syms = [f"{product.split('-')[0]}-USD"]
                resp = c.best_bid_ask(*syms, v2=False)
                recs = resp.get("results") if isinstance(resp, dict) else resp
                out["request_url"] = meta["url"] + f"?symbol={syms[0]}"
                out.update(_rows_from(list(recs or []), limit))
                out["note"] = ("bid_inclusive_of_sell_spread and ask_inclusive_of_buy_spread "
                               "are the prices you actually transact at. The gap between "
                               "them and the mid IS Robinhood's fee.")
        elif key == "news_rss":
            from app.data import news as _news
            feeds = getattr(_news, "FEEDS", [])
            recs = []
            with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as c:
                for source, url in feeds[:3]:
                    try:
                        r = c.get(url)
                        body = r.text[:4000]
                        titles = body.split("<title>")[1:4]
                        for t_ in titles:
                            recs.append({"source": source, "http_status": r.status_code,
                                         "title_raw": t_.split("</title>")[0][:160]})
                    except Exception as exc:
                        recs.append({"source": source, "http_status": None,
                                     "title_raw": f"{type(exc).__name__}: {exc}"})
            out["request_url"] = "; ".join(u for _, u in feeds[:3])
            out.update(_rows_from(recs, limit))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["elapsed_ms"] = round((time.time() - t0) * 1000)
    return out


# ── what we STORED, so it can be compared against what arrived ────────────────
LOCAL_TABLES = ["bars", "universe", "rh_spreads", "macro_bars", "news", "signals",
                "orders", "trades", "positions", "equity_curve", "scheduler_jobs",
                "events", "cost_observations"]


def local() -> list[dict]:
    out = []
    for t in LOCAL_TABLES:
        try:
            n = db.query_one(f"SELECT COUNT(*) c FROM {t}")["c"]
            cols = [r["name"] for r in db.query(f"PRAGMA table_info({t})")]
            out.append({"table": t, "rows": n, "columns": cols})
        except Exception:
            continue
    return out


def peek(table: str, limit: int = 50) -> dict:
    if table not in LOCAL_TABLES:
        return {"error": f"{table!r} is not one of the exposed tables"}
    cols = [r["name"] for r in db.query(f"PRAGMA table_info({table})")]
    order = "ts DESC" if "ts" in cols else ("id DESC" if "id" in cols else cols[0])
    rows = db.query(f"SELECT * FROM {table} ORDER BY {order} LIMIT ?", (min(limit, 500),))
    return {"table": table, "columns": cols,
            "rows": [[r[c] for c in cols] for r in rows], "row_count": len(rows)}


FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "replace",
             "attach", "detach", "pragma", "vacuum", "begin", "commit")


def query(sql: str, limit: int = 200) -> dict:
    """Read-only SQL against the local database.

    Refuses anything that is not a single SELECT. This is a research window, not
    an admin console -- the operator's own trading record lives in here.
    """
    s = (sql or "").strip().rstrip(";")
    low = s.lower()
    if not low.startswith("select") and not low.startswith("with"):
        return {"error": "only SELECT (or WITH ... SELECT) is allowed"}
    if ";" in s:
        return {"error": "one statement at a time"}
    if any(f" {w} " in f" {low} " or low.startswith(w) for w in FORBIDDEN):
        return {"error": "only read-only queries are allowed"}
    try:
        rows = db.query(f"SELECT * FROM ({s}) LIMIT {int(min(limit, 1000))}")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "sql": s}
    if not rows:
        return {"columns": [], "rows": [], "row_count": 0, "sql": s}
    cols = list(rows[0].keys())
    return {"columns": cols, "rows": [[r[c] for c in cols] for r in rows],
            "row_count": len(rows), "sql": s}
