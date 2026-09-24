"""Market data adapters.

Why not Robinhood's own data? Robinhood exposes market data only through the
agentic MCP, which is OAuth-gated, LLM-mediated, undocumented as to rate limits,
and keeps no history for us. It is fine for a quote at the moment of trading; it
is unusable as a research feed.

So: research and signals run on a public exchange feed (free, no key), and the
engine continuously measures the BASIS -- the difference between that feed and
what Robinhood actually fills at. The basis is not noise to be ignored; it IS
the cost of trading here, and it is measured in `execution/cost_model.py`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: float
    source: str

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0 or self.bid <= 0 or self.ask <= 0:
            return float("nan")
        return (self.ask - self.bid) / m * 1e4


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Feed(Protocol):
    name: str

    def products(self) -> dict[str, str]: ...
    def quote(self, product: str) -> Quote: ...
    def candles(self, product: str, granularity: int, limit: int) -> list[Bar]: ...
    def stats_24h(self, product: str) -> dict: ...


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=s.feed_timeout_s,
        headers={"User-Agent": "tradecrypto/0.1 (personal research)"},
        follow_redirects=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
class CoinbaseFeed:
    """Coinbase Exchange public API. No key, ~10 req/s soft limit."""

    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"

    def products(self) -> dict[str, str]:
        with _client() as c:
            r = c.get(f"{self.BASE}/products")
            r.raise_for_status()
            out = {}
            for p in r.json():
                if p.get("quote_currency") != "USD":
                    continue
                if p.get("trading_disabled") or p.get("status") != "online":
                    continue
                out[p["base_currency"].upper()] = p["id"]
            return out

    def quote(self, product: str) -> Quote:
        with _client() as c:
            r = c.get(f"{self.BASE}/products/{product}/ticker")
            r.raise_for_status()
            d = r.json()
        return Quote(
            symbol=product.split("-")[0],
            bid=float(d.get("bid") or 0),
            ask=float(d.get("ask") or 0),
            last=float(d.get("price") or 0),
            ts=time.time(),
            source=self.name,
        )

    def candles(self, product: str, granularity: int = 60, limit: int = 300) -> list[Bar]:
        # Coinbase returns at most 300 candles, newest first:
        # [time, low, high, open, close, volume]
        with _client() as c:
            r = c.get(
                f"{self.BASE}/products/{product}/candles",
                params={"granularity": granularity},
            )
            r.raise_for_status()
            rows = r.json()
        bars = [
            Bar(int(x[0]), float(x[3]), float(x[2]), float(x[1]), float(x[4]), float(x[5]))
            for x in rows
        ]
        bars.sort(key=lambda b: b.ts)
        return bars[-limit:]

    def stats_24h(self, product: str) -> dict:
        with _client() as c:
            r = c.get(f"{self.BASE}/products/{product}/stats")
            r.raise_for_status()
            d = r.json()
        open_, last = float(d.get("open") or 0), float(d.get("last") or 0)
        return {
            "open": open_,
            "last": last,
            "high": float(d.get("high") or 0),
            "low": float(d.get("low") or 0),
            "volume_base": float(d.get("volume") or 0),
            "change_pct": ((last - open_) / open_ * 100) if open_ else float("nan"),
        }


# ──────────────────────────────────────────────────────────────────────────────
class KrakenFeed:
    """Kraken public API. Used as fallback and as an independent cross-check:
    if two venues disagree materially on a price, we do not trade that symbol."""

    name = "kraken"
    BASE = "https://api.kraken.com/0/public"

    def products(self) -> dict[str, str]:
        with _client() as c:
            r = c.get(f"{self.BASE}/AssetPairs")
            r.raise_for_status()
            d = r.json().get("result", {})
        out = {}
        for key, p in d.items():
            if p.get("quote") not in {"ZUSD", "USD"}:
                continue
            if p.get("status") != "online":
                continue
            base = (p.get("wsname", "/").split("/")[0] or "").upper()
            base = {"XBT": "BTC", "XDG": "DOGE"}.get(base, base)
            if base:
                out[base] = key
        return out

    def quote(self, product: str) -> Quote:
        with _client() as c:
            r = c.get(f"{self.BASE}/Ticker", params={"pair": product})
            r.raise_for_status()
            res = r.json().get("result", {})
        if not res:
            raise ValueError(f"kraken: no ticker for {product}")
        d = next(iter(res.values()))
        return Quote(
            symbol=product,
            bid=float(d["b"][0]),
            ask=float(d["a"][0]),
            last=float(d["c"][0]),
            ts=time.time(),
            source=self.name,
        )

    def candles(self, product: str, granularity: int = 60, limit: int = 300) -> list[Bar]:
        interval = max(1, granularity // 60)
        with _client() as c:
            r = c.get(f"{self.BASE}/OHLC", params={"pair": product, "interval": interval})
            r.raise_for_status()
            res = r.json().get("result", {})
        rows = next((v for k, v in res.items() if k != "last"), [])
        bars = [
            Bar(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6]))
            for x in rows
        ]
        return bars[-limit:]

    def stats_24h(self, product: str) -> dict:
        with _client() as c:
            r = c.get(f"{self.BASE}/Ticker", params={"pair": product})
            r.raise_for_status()
            res = r.json().get("result", {})
        d = next(iter(res.values()))
        open_, last = float(d["o"]), float(d["c"][0])
        return {
            "open": open_,
            "last": last,
            "high": float(d["h"][1]),
            "low": float(d["l"][1]),
            "volume_base": float(d["v"][1]),
            "change_pct": ((last - open_) / open_ * 100) if open_ else float("nan"),
        }


_FEEDS: dict[str, Feed] = {"coinbase": CoinbaseFeed(), "kraken": KrakenFeed()}


def get_feed(name: str | None = None) -> Feed:
    s = get_settings()
    return _FEEDS[(name or s.primary_feed).lower()]


def get_fallback_feed() -> Feed:
    return _FEEDS[get_settings().fallback_feed.lower()]


def cross_check(symbol: str, primary: Quote, secondary: Quote, tol_bps: float = 50.0) -> dict:
    """Two independent venues must agree before we act on a price.

    A stale or wrong quote is the cheapest way to lose money automatically, and
    a single feed gives you no way to notice.
    """
    if primary.mid <= 0 or secondary.mid <= 0:
        return {"agree": False, "reason": "non-positive mid", "diff_bps": None}
    diff = abs(primary.mid - secondary.mid) / primary.mid * 1e4
    return {
        "agree": bool(diff <= tol_bps),
        "diff_bps": float(diff),
        "tolerance_bps": tol_bps,
        "primary": {"source": primary.source, "mid": primary.mid},
        "secondary": {"source": secondary.source, "mid": secondary.mid},
        "reason": None if diff <= tol_bps else f"venues disagree by {diff:.1f} bps",
    }
