"""Prediction-market prices on crypto, as one more piece of MARKET CONTEXT.

Polymarket runs a ladder of same-day markets -- "Will the price of Bitcoin be
above $86,000 on September 21?" at $2,000 steps -- plus monthly reach/dip
markets and year-end targets, for BTC and ETH (occasionally SOL). Each price is
the crowd's probability. Read together, a same-day ladder is a distribution
of where BTC closes today: the strike where the probability crosses one half
is the implied median, and the width of the ladder between 10% and 90% is the
implied range. That is the same information an options desk reads off the
term structure, from a source that is free and needs no key (Gamma API).

What this is for, and what it is not for (operator's question, 2026-09-21):

  - It is a MACRO / REGIME input: "what does the crowd expect of BTC today
    and this month". It goes next to breadth, BTC-24h and dispersion in the
    regime read, and it is judged there, by the same 2-sigma bar as everything
    else, before it changes a single size.
  - It is NOT an entry signal for an altcoin burst. The markets cover BTC and
    ETH, they resolve at day/month ends, and a 15-minute move on SEI is not in
    them. Nothing in the execution, strategy or risk path reads this table.

Storage: `prediction_markets` rows, one per market per fetch, so the history
of the crowd's view is kept and can be replayed against what happened.
Gamma refuses the default Python user-agent (403); a plain named UA is fine.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import httpx

from app.core import db

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CRYPTO_TAG_ID = 21
USER_AGENT = "tradecrypto/1.0 (research; read-only)"

_ASSETS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH",
           "solana": "SOL", "sol": "SOL", "xrp": "XRP", "dogecoin": "DOGE"}
_MONEY = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)")


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS prediction_markets (
        ts REAL NOT NULL, source TEXT NOT NULL, market_id TEXT NOT NULL,
        asset TEXT, kind TEXT, threshold REAL, end_date TEXT,
        question TEXT, p_yes REAL, volume24h REAL, liquidity REAL,
        PRIMARY KEY (ts, source, market_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_pm_asset_end ON prediction_markets(asset, end_date, ts)")


def _threshold(q: str) -> float | None:
    m = _MONEY.search(q)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    suf = m.group(2).lower()
    return v * (1e3 if suf == "k" else 1e6 if suf == "m" else 1.0)


def classify(question: str) -> dict:
    """asset / kind / threshold from the question text alone (no network)."""
    q = question.strip()
    low = q.lower()
    asset = next((sym for word, sym in _ASSETS.items() if re.search(rf"\b{word}\b", low)), None)
    thr = _threshold(q)
    if re.search(r"\bup or down\b", low):
        kind = "up_or_down"
    elif re.search(r"\bprice of .* be above\b", low) or re.search(r"\babove \$", low):
        kind = "above_on_day"
    elif re.search(r"\bbelow \$", low):
        kind = "below_on_day"
    elif re.search(r"\b(reach|hit)\b", low):
        kind = "reach_by"
    elif re.search(r"\b(dip|fall|drop)\b", low):
        kind = "dip_to"
    else:
        kind = "other"
    return {"asset": asset, "kind": kind, "threshold": thr}


def parse_market(m: dict) -> dict | None:
    """One Gamma market row -> our row. None when it carries no usable price."""
    q = m.get("question") or ""
    if not q:
        return None
    prices = m.get("outcomePrices")
    try:
        if isinstance(prices, str):
            prices = json.loads(prices)
        p_yes = float(prices[0])
    except Exception:
        return None
    c = classify(q)
    end = str(m.get("endDate") or "")[:10] or None
    return {"market_id": str(m.get("id")), "question": q, "p_yes": p_yes,
            "volume24h": float(m.get("volume24hr") or 0.0),
            "liquidity": float(m.get("liquidity") or 0.0),
            "end_date": end, **c}


def fetch(limit: int = 100, timeout: float = 15.0, client: httpx.Client | None = None) -> dict:
    """Pull the busiest crypto markets and store one row each, stamped now."""
    ensure_schema()
    params = {"active": "true", "closed": "false", "limit": str(limit),
              "tag_id": str(CRYPTO_TAG_ID), "order": "volume24hr", "ascending": "false"}
    own = client is None
    client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        r = client.get(GAMMA_URL, params=params)
        r.raise_for_status()
        raw = r.json()
    except Exception as exc:
        return {"ok": False, "stored": 0, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    finally:
        if own:
            client.close()
    now = time.time()
    rows = [p for p in (parse_market(m) for m in (raw or [])) if p]
    db.executemany(
        "INSERT OR REPLACE INTO prediction_markets(ts, source, market_id, asset, kind, threshold, "
        "end_date, question, p_yes, volume24h, liquidity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(now, "polymarket", p["market_id"], p["asset"], p["kind"], p["threshold"], p["end_date"],
          p["question"], p["p_yes"], p["volume24h"], p["liquidity"]) for p in rows])
    return {"ok": True, "stored": len(rows), "fetched": len(raw or []), "ts": now}


def implied_from_ladder(rows: list[dict]) -> dict | None:
    """A same-day 'above $X' ladder -> implied median and 10/90 range.

    P(close > X) falls as X rises. The median is where it crosses 0.5,
    interpolated between neighbouring strikes; the 10th/90th percentiles the
    same way at 0.9 and 0.1. Needs at least three strikes with a crossing.
    """
    pts = sorted(((float(r["threshold"]), float(r["p_yes"])) for r in rows
                  if r.get("threshold") and r.get("p_yes") is not None), key=lambda t: t[0])
    if len(pts) < 3:
        return None

    def cross(level: float) -> float | None:
        for (x0, p0), (x1, p1) in zip(pts, pts[1:]):
            if (p0 - level) * (p1 - level) <= 0 and p0 != p1:
                return x0 + (x1 - x0) * (p0 - level) / (p0 - p1)
        return None

    med, lo, hi = cross(0.5), cross(0.9), cross(0.1)
    if med is None:
        return None
    return {"median": med, "p10": lo, "p90": hi, "strikes": len(pts),
            "ladder": [{"threshold": x, "p_above": p} for x, p in pts]}


def latest(assets: tuple[str, ...] = ("BTC", "ETH")) -> dict:
    """The newest fetch, read as: today's implied distribution per asset, plus
    the busiest longer-dated markets. No network."""
    ensure_schema()
    row = db.query_one("SELECT MAX(ts) t FROM prediction_markets WHERE source='polymarket'")
    if not row or not row["t"]:
        return {"available": False, "why": "no fetch yet — the prediction_markets job runs hourly"}
    ts = float(row["t"])
    rows = db.query("SELECT * FROM prediction_markets WHERE source='polymarket' AND ts=? "
                    "ORDER BY volume24h DESC", (ts,))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {"available": True, "fetched_at": ts, "n_markets": len(rows), "today": {}, "longer_dated": []}
    for a in assets:
        ladder = [r for r in rows if r["asset"] == a and r["kind"] == "above_on_day"
                  and r["end_date"] and r["end_date"] >= today]
        if not ladder:
            continue
        nearest = min(r["end_date"] for r in ladder)
        imp = implied_from_ladder([r for r in ladder if r["end_date"] == nearest])
        if imp:
            out["today"][a] = {"resolves": nearest, **imp}
    for r in rows:
        if r["kind"] in ("reach_by", "dip_to") and r["asset"]:
            out["longer_dated"].append({k: r[k] for k in ("asset", "kind", "threshold", "end_date",
                                                          "question", "p_yes", "volume24h")})
        if len(out["longer_dated"]) >= 12:
            break
    out["note"] = ("Crowd probabilities from Polymarket's crypto markets. Context for the regime "
                   "read (BTC/ETH only, day and month horizons) — not an entry input for any "
                   "strategy, and nothing in the execution path reads it.")
    return out
