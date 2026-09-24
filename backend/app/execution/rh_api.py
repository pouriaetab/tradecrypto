"""Robinhood Crypto Trading API — the real one, over REST.

Why this exists
---------------
For several sessions this project believed Robinhood offered no data and that an
LLM-mediated MCP agent was the only way to reach them. Both were wrong. Robinhood
has published a Crypto Trading REST API since May 2024 with market data, account,
holdings and orders. Nobody here had wired it up, and "our app has no Robinhood
data" got restated as "Robinhood gives no data" until it hardened into a fact.

Spec read from https://docs.robinhood.com/crypto/trading/ on 2026-09-07.

What it settles
---------------
* is_api_tradable on the trading-pairs endpoint answers which coins can actually
  be traded — the `rh_confirmed` column has been 0 for all 50 coins since day one.
* best_bid_ask gives Robinhood's own bid/ask, so the price basis against Coinbase
  stops being an assumption.
* estimated_price gives the expected fill for a given size, which is the spread
  measured rather than inferred.
* orders can be placed without any language model, so the bot's token cost is zero.

v1 versus v2 — they are different cost models, not versions
------------------------------------------------------------
v1 routes to MARKET MAKERS. The quoted bid and ask already contain the spread
(0.95% each side for DOGE, from the order ticket), and v1 orders do not count
toward fee-tier volume.

v2 routes to PARTNER EXCHANGES and charges an explicit fee by 30-day volume tier.
From Robinhood's fee schedule (rhc-fee-schedule.pdf, read 2026-09-07):

    30-day volume     taker     maker
    $0-10K            0.95%     0.50%
    $10K-50K          0.75%     0.35%
    $50K-250K         0.25%     0.125%
    $250K-500K        0.15%     0.075%
    $500K-1M          0.125%    0.06%

Two things follow, and they point in opposite directions:

  * At the entry tier the v2 TAKER fee is 0.95% — identical to the spread. And
    Robinhood states that orders through the v2 API "are charged the taker rate
    until maker/taker is fully rolled out". So today the API buys us no cost
    improvement at all.
  * But the tiers fall steeply. At $50K of 30-day volume the taker rate drops to
    0.25%, a 50 bps round trip — cheaper than Kraken Pro's entry tier. With a
    $500 account, $50K of monthly volume is roughly 50 round trips a month.

So the cost is a function of activity, which no venue comparison built on a flat
fee can capture. That is worth knowing before choosing a venue.

Signing, and a trap in Robinhood's own example
-----------------------------------------------
    message = f"{api_key}{timestamp}{path}{method}{body}"

signed with Ed25519, base64-encoded, sent as x-signature alongside x-api-key and
x-timestamp. Timestamps expire after 30 seconds.

The trap: Robinhood's sample client signs `json.dumps(body)` — which inserts a
space after every comma and colon — and then sends the request with `json=...`,
letting the HTTP library re-serialise it compactly. The signed bytes and the sent
bytes are therefore not the same string. That may work if the server re-parses
before verifying, but it is not something to rely on. Here the body is serialised
ONCE and the identical string is both signed and sent as the raw request body.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.core import db

BASE_URL = "https://trading.robinhood.com"
RATE_LIMIT_PER_MIN = 100          # documented; bursts to 300
TIMESTAMP_VALID_S = 30

# Robinhood's published fee tiers for exchange routing (v2). Verified from
# cdn.robinhood.com/assets/robinhood/legal/rhc-fee-schedule.pdf on 2026-09-07.
# Re-read from the PDF on 2026-09-14 (document dated 2026-06-22). The previous
# copy of this table stopped at $5M and was missing the top two tiers.
#
# These fees apply ONLY to EXCHANGE ROUTING (orders sent to EDX Markets and
# Bitstamp USA). Robinhood's DEFAULT is Market Maker Routing, which charges no
# commission at all and takes an embedded spread instead -- "for every $100 of
# notional crypto order volume executed through market maker routing, Robinhood
# Crypto receives $0.95". That is the 0.95%/side this desk has been measuring.
#
# The tier is set by trailing 30-day volume OF EXCHANGE-ROUTED ORDERS ONLY, and
# is assigned at the moment the order is placed.
FEE_TIERS = [
    {"min_volume_usd": 0,         "taker_pct": 0.95,  "maker_pct": 0.50},
    {"min_volume_usd": 10_000,    "taker_pct": 0.75,  "maker_pct": 0.35},
    {"min_volume_usd": 50_000,    "taker_pct": 0.25,  "maker_pct": 0.125},
    {"min_volume_usd": 250_000,   "taker_pct": 0.15,  "maker_pct": 0.075},
    {"min_volume_usd": 500_000,   "taker_pct": 0.125, "maker_pct": 0.06},
    {"min_volume_usd": 1_000_000, "taker_pct": 0.10,  "maker_pct": 0.04},
    {"min_volume_usd": 5_000_000, "taker_pct": 0.04,  "maker_pct": 0.02},
    {"min_volume_usd": 10_000_000, "taker_pct": 0.03, "maker_pct": 0.01},
    {"min_volume_usd": 25_000_000, "taker_pct": 0.03, "maker_pct": 0.00},
]


def fee_for_volume(volume_usd: float, style: str = "taker") -> float:
    """Percentage fee per side at a given trailing 30-day volume."""
    tier = FEE_TIERS[0]
    for t in FEE_TIERS:
        if volume_usd >= t["min_volume_usd"]:
            tier = t
    return tier["taker_pct"] if style == "taker" else tier["maker_pct"]


class NotConfigured(RuntimeError):
    """No API credentials. Not an error condition — the normal starting state."""


class RobinhoodCrypto:
    """Read-only by default. Order placement is gated by the live interlock."""

    def __init__(self, api_key: str | None = None, private_key_b64: str | None = None):
        s = get_settings()
        self.s = s
        self.api_key = api_key or getattr(s, "rh_api_key", "") or ""
        priv = private_key_b64 or getattr(s, "rh_private_key", "") or ""
        if not (self.api_key and priv):
            creds = self._from_file()
            self.api_key = self.api_key or creds.get("api_key", "")
            priv = priv or creds.get("private_key", "")
        self._priv_b64 = priv
        self._signer = None
        self._calls: list[float] = []

    # ── credentials ──────────────────────────────────────────────────────────
    def _from_file(self) -> dict:
        p = self.s.rh_credentials_file
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self._priv_b64)

    def _signing_key(self):
        if self._signer is None:
            try:
                from nacl.signing import SigningKey
            except ImportError:
                raise NotConfigured(
                    "PyNaCl is not installed. Run:  backend/.venv/bin/pip install pynacl"
                ) from None
            self._signer = SigningKey(base64.b64decode(self._priv_b64))
        return self._signer

    # ── request plumbing ─────────────────────────────────────────────────────
    def _throttle(self) -> None:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 60]
        if len(self._calls) >= RATE_LIMIT_PER_MIN - 5:     # leave headroom
            time.sleep(max(0.0, 60 - (now - self._calls[0])) + 0.1)
        self._calls.append(time.time())

    def _headers(self, method: str, path: str, body: str) -> dict:
        ts = int(time.time())
        message = f"{self.api_key}{ts}{path}{method}{body}"
        sig = self._signing_key().sign(message.encode("utf-8")).signature
        return {
            "x-api-key": self.api_key,
            "x-signature": base64.b64encode(sig).decode("utf-8"),
            "x-timestamp": str(ts),
            "Content-Type": "application/json; charset=utf-8",
        }

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        if not self.configured:
            raise NotConfigured(
                "No Robinhood API credentials. Create them in crypto account "
                "settings on web classic, then save them — see docs/ROBINHOOD_API.md.")
        # Serialise exactly once: the bytes we sign are the bytes we send.
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        self._throttle()
        headers = self._headers(method, path, body)
        url = BASE_URL + path
        with httpx.Client(timeout=15.0) as c:
            r = (c.get(url, headers=headers) if method == "GET"
                 else c.post(url, headers=headers, content=body.encode("utf-8")))
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = {"raw": r.text[:400]}
            raise RuntimeError(f"Robinhood {method} {path} -> {r.status_code}: {detail}")
        return r.json()

    # ── read-only ────────────────────────────────────────────────────────────
    def accounts(self, v2: bool = True) -> Any:
        return self.request("GET", f"/api/v{'2' if v2 else '1'}/crypto/trading/accounts/")

    def account_number(self) -> str:
        a = self.accounts(v2=True)
        rows = a.get("results") or []
        if not rows:
            raise RuntimeError("no crypto accounts returned")
        return rows[0]["account_number"]

    def trading_pairs(self, *symbols: str, v2: bool = True) -> list[dict]:
        """Every pair Robinhood lists, with is_api_tradable — the answer to which
        coins we may actually trade. Paginated; this walks every page."""
        ver = "2" if v2 else "1"
        q = "".join(f"symbol={s}&" for s in symbols)
        path = f"/api/v{ver}/crypto/trading/trading_pairs/" + (f"?{q[:-1]}" if q else "")
        out: list[dict] = []
        while path:
            resp = self.request("GET", path)
            out.extend(resp.get("results", []))
            nxt = resp.get("next")
            path = nxt.replace(BASE_URL, "") if nxt else None
        return out

    def best_bid_ask(self, *symbols: str, v2: bool = True) -> Any:
        ver = "2" if v2 else "1"
        q = "&".join(f"symbol={s}" for s in symbols)
        return self.request("GET", f"/api/v{ver}/crypto/marketdata/best_bid_ask/?{q}")

    def estimated_price(self, symbol: str, side: str = "both",
                        quantity: str = "1", v2: bool = True) -> Any:
        """Expected execution price for a given size — the spread, measured."""
        if v2:
            path = (f"/api/v2/crypto/trading/estimated_price/"
                    f"?symbol={symbol}&side={side}&quantity={quantity}")
        else:
            path = (f"/api/v1/crypto/marketdata/estimated_price/"
                    f"?symbol={symbol}&side={side}&quantity={quantity}")
        return self.request("GET", path)

    def holdings(self, account_number: str | None = None, *asset_codes: str) -> Any:
        if account_number:
            q = f"?account_number={account_number}" + "".join(
                f"&asset_code={a}" for a in asset_codes)
            return self.request("GET", f"/api/v2/crypto/trading/holdings/{q}")
        q = "?" + "&".join(f"asset_code={a}" for a in asset_codes) if asset_codes else ""
        return self.request("GET", f"/api/v1/crypto/trading/holdings/{q}")

    def orders(self, account_number: str | None = None) -> Any:
        if account_number:
            return self.request(
                "GET", f"/api/v2/crypto/trading/orders/?account_number={account_number}")
        return self.request("GET", "/api/v1/crypto/trading/orders/")

    # ── writes: interlocked ──────────────────────────────────────────────────
    def place_order(self, symbol: str, side: str, order_type: str,
                    config: dict, account_number: str | None = None,
                    client_order_id: str | None = None) -> Any:
        """Place a real order. Refuses unless BOTH live conditions hold.

        This is the only method here that can move money, so it re-checks the
        interlock itself rather than trusting a caller to have done it.
        """
        if not self.s.live_enabled:
            raise PermissionError(
                "live trading is not enabled: needs TC_EXECUTION_MODE=mcp and "
                "TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK. Refusing to place an order.")
        if order_type not in ("market", "limit", "stop_loss", "stop_limit"):
            raise ValueError(f"unknown order type {order_type!r}")
        payload = {
            "symbol": symbol.upper(),
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "type": order_type,
            f"{order_type}_order_config": config,
        }
        acct = account_number or self.account_number()
        path = f"/api/v2/crypto/trading/orders/?account_number={acct}"
        db.log_event("WARN", "broker",
                     f"placing REAL {order_type} {side} order: {symbol} {config}")
        return self.request("POST", path, payload)

    def cancel(self, order_id: str) -> Any:
        return self.request("POST", f"/api/v2/crypto/trading/orders/{order_id}/cancel/")

    # ── diagnostics ──────────────────────────────────────────────────────────
    def probe(self) -> dict:
        """Can we talk to Robinhood, and what does it say? Never places an order."""
        out: dict[str, Any] = {
            "configured": self.configured,
            "base_url": BASE_URL,
            "live_enabled": self.s.live_enabled,
        }
        try:
            import nacl  # noqa: F401
            out["pynacl_installed"] = True
        except ImportError:
            out["pynacl_installed"] = False
            out["fix"] = "backend/.venv/bin/pip install pynacl"
        if not self.configured:
            out["next_step"] = (
                "Create API credentials in Robinhood crypto account settings on web "
                "classic, then save them to secrets/robinhood_api.json as "
                '{"api_key": "rh-api-…", "private_key": "<base64 Ed25519 seed>"}')
            return out
        if not out.get("pynacl_installed"):
            return out
        try:
            acct = self.accounts()
            rows = acct.get("results") or [acct]
            out["reachable"] = True
            out["account"] = {k: rows[0].get(k) for k in
                              ("account_number", "status", "buying_power",
                               "buying_power_currency") if k in rows[0]}
        except Exception as exc:
            out["reachable"] = False
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out


# ── sync: what only Robinhood can tell us ────────────────────────────────────
def sync_universe(client: "RobinhoodCrypto | None" = None) -> dict:
    """Fill in which coins Robinhood will actually let the API trade.

    `universe.rh_confirmed` has been 0 for all 50 coins since the project began,
    because nothing ever asked Robinhood. The trading-pairs endpoint carries
    `is_api_tradable`, which is the authoritative answer — and it is stricter
    than "is listed": a coin can be visible in the app and still be refused by
    the API.
    """
    c = client or RobinhoodCrypto()
    pairs = c.trading_pairs()
    tradable, listed = set(), set()
    # Robinhood publishes a per-pair `min_order_size` in base units. That number
    # is the venue's own floor on how small a position may be, and it is what
    # ends a day's trading when cash runs low -- so it gets stored rather than
    # thrown away, and nothing in this repo has to invent a minimum.
    minima: dict[str, float] = {}
    for p in pairs:
        sym = (p.get("symbol") or "").upper()
        base = sym.split("-")[0]
        if not base:
            continue
        listed.add(base)
        if p.get("is_api_tradable") and sym.endswith("-USD"):
            tradable.add(base)
            for key in ("min_order_size", "min_order_quantity", "minOrderSize"):
                v = p.get(key)
                if v not in (None, ""):
                    try:
                        minima[base] = float(v)
                    except (TypeError, ValueError):
                        pass
                    break

    ours = [r["symbol"] for r in db.query("SELECT symbol FROM universe")]
    confirmed = deactivated = 0
    priced = 0
    for sym in ours:
        ok = sym in tradable
        db.execute("UPDATE universe SET rh_confirmed=? WHERE symbol=?", (1 if ok else 0, sym))
        confirmed += ok
        if not ok:
            deactivated += 1
        base_min = minima.get(sym)
        if ok and base_min and base_min > 0:
            # base units -> dollars, at the last price we have for the coin.
            px = db.query_one(
                "SELECT close FROM bars WHERE symbol=? ORDER BY ts DESC LIMIT 1", (sym,))
            if px and px["close"]:
                db.execute("UPDATE universe SET min_notional=? WHERE symbol=?",
                           (float(base_min) * float(px["close"]), sym))
                priced += 1
    ours_not_tradable = sorted(set(ours) - tradable)
    tradable_not_tracked = sorted(tradable - set(ours))
    db.log_event("INFO", "universe",
                 f"Robinhood sync: {confirmed}/{len(ours)} tracked coins are API-tradable; "
                 f"{len(tradable)} tradable pairs listed overall")
    return {
        "pairs_returned": len(pairs),
        "api_tradable_usd_pairs": len(tradable),
        "listed_bases": len(listed),
        "our_universe": len(ours),
        "confirmed": confirmed,
        "min_notional_priced": priced,
        "not_tradable": ours_not_tradable,
        "tradable_but_not_tracked": tradable_not_tracked,
        "explains_the_count": (
            f"Robinhood's API will trade {len(tradable)} USD pairs. We track "
            f"{len(ours)} coins, and {confirmed} of those are on that list — that "
            f"is the number you see, not a limit Robinhood imposed. "
            f"{len(ours_not_tradable)} coins we track are NOT API-tradable "
            f"({', '.join(ours_not_tradable[:12])}"
            f"{'…' if len(ours_not_tradable) > 12 else ''}), and "
            f"{len(tradable_not_tracked)} coins Robinhood would trade are simply "
            f"not in our universe yet."),
        "note": ("is_api_tradable is stricter than 'visible in the app'. A coin can "
                 "be tradable by hand on the website and still be refused by the "
                 "API, which is why the app's count and this one differ."),
    }


def fee_status(client: "RobinhoodCrypto | None" = None) -> dict:
    """Your actual fee tier and 30-day volume, straight from the account.

    The accounts endpoint carries `thirty_day_volume`, `fee_tier_status` and
    `next_fee_tier_threshold`. That turns the fee-tier table from something read
    off a PDF into something measured against where you actually stand.
    """
    c = client or RobinhoodCrypto()
    resp = c.accounts(v2=True)
    rows = resp.get("results") if isinstance(resp, dict) else None
    row = (rows or [resp])[0] if (rows or isinstance(resp, dict)) else {}

    def num(*names):
        for k in names:
            v = row.get(k)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    vol = num("thirty_day_volume") or 0.0
    taker, maker = fee_for_volume(vol, "taker"), fee_for_volume(vol, "maker")
    nxt = num("next_fee_tier_threshold")
    return {
        "raw_fields": sorted(row) if isinstance(row, dict) else [],
        "account_number": row.get("account_number"),
        "buying_power": row.get("buying_power"),
        "thirty_day_volume_usd": vol,
        "fee_tier_status": row.get("fee_tier_status"),
        "taker_pct": taker, "maker_pct": maker,
        "round_trip_taker_bps": taker * 200,
        "round_trip_maker_bps": maker * 200,
        "next_tier_threshold_usd": nxt,
        "note": (f"At ${vol:,.0f} of trailing 30-day volume you pay {taker:.2f}% "
                 f"taker — a {taker*200:.0f} bps round trip. The v2 API is charged "
                 f"the taker rate until Robinhood finishes rolling out maker/taker."
                 + (f" Next tier at ${nxt:,.0f}." if nxt else "")),
    }


def measure_spreads(symbols: list[str] | None = None,
                    client: "RobinhoodCrypto | None" = None) -> dict:
    """Replace the assumed 0.95% with Robinhood's own numbers, per coin.

    Uses the **v1** market-data endpoint, deliberately. v1 routes to market
    makers and returns the spread itself — `buy_spread`, `sell_spread`,
    `bid_inclusive_of_sell_spread`, `ask_inclusive_of_buy_spread`. v2 routes to
    exchanges and charges a fee instead, so it has no spread fields at all.
    Calling v2 here returned rows this code did not recognise and skipped every
    one of them without a word: 0 measured, 0 errors, nothing to debug.

    Nothing is skipped silently any more. A row we cannot read is reported with
    the field names it actually had.
    """
    from app.data import price_basis
    from app.execution import rh_spread

    c = client or RobinhoodCrypto()
    if symbols is None:
        symbols = [r["symbol"] for r in db.query(
            "SELECT symbol FROM universe WHERE active=1 AND rh_confirmed=1 ORDER BY symbol")]
    if not symbols:
        return {"error": "no API-tradable coins on record — run the universe sync first",
                "measured": [], "n": 0}

    out, errors, unreadable = [], [], []
    for batch in [symbols[i:i + 10] for i in range(0, len(symbols), 10)]:
        pairs = [f"{s}-USD" for s in batch]
        try:
            resp = c.best_bid_ask(*pairs, v2=False)      # v1: the one with spreads
        except Exception as exc:
            errors.append(f"{','.join(batch)}: {type(exc).__name__}: {exc}")
            continue
        rows = resp.get("results") if isinstance(resp, dict) else None
        if not rows:
            errors.append(f"{','.join(batch)}: no 'results' in the response "
                          f"(top-level keys: {sorted(resp) if isinstance(resp, dict) else type(resp).__name__})")
            continue
        for row in rows:
            sym = (row.get("symbol") or "").split("-")[0]
            def num(*names):
                for k in names:
                    v = row.get(k)
                    if v not in (None, ""):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return None

            mid = num("price", "mid_price")
            bid = num("bid_inclusive_of_sell_spread", "bid", "bid_price")
            ask = num("ask_inclusive_of_buy_spread", "ask", "ask_price")
            # Robinhood states the spread directly; prefer it over our arithmetic.
            buy_pct = num("buy_spread")
            sell_pct = num("sell_spread")
            if buy_pct is not None and buy_pct < 1:        # sometimes a ratio, not a %
                buy_pct *= 100
            if sell_pct is not None and sell_pct < 1:
                sell_pct *= 100
            if (buy_pct is None or sell_pct is None) and mid and bid and ask:
                buy_pct = (ask - mid) / mid * 100
                sell_pct = (mid - bid) / mid * 100
            if buy_pct is None or sell_pct is None or not mid:
                unreadable.append({"symbol": sym or "?", "fields_returned": sorted(row)})
                continue

            per_side = (buy_pct + sell_pct) / 2
            try:
                rh_spread.set_spread(
                    sym, round(per_side, 4), source="observed",
                    note=(f"Robinhood v1 quote: mid {mid:.8g}, buy spread {buy_pct:.3f}%, "
                          f"sell spread {sell_pct:.3f}%"))
            except Exception as exc:
                errors.append(f"{sym}: {exc}")
                continue
            basis = price_basis.record(sym, mid, note="Robinhood API best_bid_ask (v1)")
            out.append({"symbol": sym, "mid": mid, "bid": bid, "ask": ask,
                        "buy_spread_pct": buy_pct, "sell_spread_pct": sell_pct,
                        "per_side_pct": per_side,
                        "round_trip_pct": rh_spread.round_trip_bps(per_side) / 100,
                        "basis_vs_our_feed_bps": basis.get("basis_bps")})

    cheapest = min(out, key=lambda r: r["per_side_pct"]) if out else None
    dearest = max(out, key=lambda r: r["per_side_pct"]) if out else None
    return {
        "measured": out, "n": len(out),
        "asked_for": len(symbols),
        "errors": errors,
        "unreadable": unreadable,
        "cheapest": cheapest, "dearest": dearest,
        "note": (f"{len(out)} of {len(symbols)} coins now have a spread read from "
                 f"Robinhood rather than assumed."
                 + (f" Cheapest {cheapest['symbol']} at {cheapest['per_side_pct']:.3f}% "
                    f"per side; dearest {dearest['symbol']} at "
                    f"{dearest['per_side_pct']:.3f}%." if cheapest else "")
                 + (f" {len(unreadable)} rows could not be parsed — their field names "
                    f"are listed above." if unreadable else "")),
    }


def routing_comparison(symbols: list[str] | None = None,
                       client: "RobinhoodCrypto | None" = None) -> dict:
    """What does each routing choice actually cost? Read-only, no order placed.

    The open question was whether a limit order escapes Robinhood's 0.95%
    markup, and the assumption was that answering it required risking real money.
    It does not. Robinhood publishes both quotes:

      v1  market-maker routing. The docs say plainly: "The bid and ask prices
          include a spread. The buy spread is the percent difference between the
          ask and the mid price."  ->  the marked-up price.

      v2  exchange routing. "the best available price across our partner
          exchanges. This price does not take into account the order size or
          fee."  ->  the raw exchange price, before Robinhood's fee.

    Ask for both, at the same instant, for the same coin. The difference between
    them IS the markup, measured rather than inferred. Then add the published
    fee-tier rate to the v2 side and compare like for like.

    A $5 trade would have answered this. So does a GET request.
    """
    c = client or RobinhoodCrypto()
    if symbols is None:
        symbols = [r["symbol"] for r in db.query(
            "SELECT symbol FROM universe WHERE active=1 AND rh_confirmed=1 "
            "ORDER BY symbol LIMIT 12")]
    if not symbols:
        return {"error": "no API-tradable coins on record — run the universe sync first"}

    try:
        vol = float((fee_status(c) or {}).get("thirty_day_volume_usd") or 0.0)
    except Exception:
        vol = 0.0
    taker_pct = fee_for_volume(vol, "taker")
    maker_pct = fee_for_volume(vol, "maker")

    rows, errors = [], []
    for sym in symbols:
        pair = f"{sym}-USD"
        try:
            v1 = c.best_bid_ask(pair, v2=False)
            v2 = c.best_bid_ask(pair, v2=True)
        except Exception as exc:
            errors.append(f"{sym}: {type(exc).__name__}: {exc}")
            continue

        def pick(resp):
            r = (resp.get("results") or [{}])[0] if isinstance(resp, dict) else {}
            def num(*names):
                for k in names:
                    v = r.get(k)
                    if v not in (None, ""):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                return None
            return (num("price", "mid_price"),
                    num("bid_inclusive_of_sell_spread", "bid", "bid_price"),
                    num("ask_inclusive_of_buy_spread", "ask", "ask_price"),
                    sorted(r) if isinstance(r, dict) else [])

        m1, b1, a1, f1 = pick(v1)
        m2, b2, a2, f2 = pick(v2)
        if not (m1 and b1 and a1):
            errors.append(f"{sym}: v1 quote unreadable, fields were {f1}")
            continue
        if not (m2 and b2 and a2):
            errors.append(f"{sym}: v2 quote unreadable, fields were {f2}")
            continue

        # Market-maker route: the whole cost is baked into the quote.
        mm_rt_bps = (a1 / b1 - 1) * 1e4
        # Exchange route: the book's own spread, plus the fee twice.
        ex_book_bps = (a2 / b2 - 1) * 1e4
        ex_rt_taker = ex_book_bps + 2 * taker_pct * 100
        ex_rt_maker = ex_book_bps + 2 * maker_pct * 100

        rows.append({
            "symbol": sym,
            "market_maker_round_trip_bps": mm_rt_bps,
            "exchange_book_spread_bps": ex_book_bps,
            "exchange_round_trip_taker_bps": ex_rt_taker,
            "exchange_round_trip_maker_bps": ex_rt_maker,
            "taker_saves_bps": mm_rt_bps - ex_rt_taker,
            "maker_saves_bps": mm_rt_bps - ex_rt_maker,
            "cheapest": min(
                [("market maker", mm_rt_bps), ("exchange taker", ex_rt_taker),
                 ("exchange maker", ex_rt_maker)], key=lambda x: x[1])[0],
        })

    if not rows:
        return {"error": "no readable quotes", "errors": errors}

    def avg(k):
        return sum(r[k] for r in rows) / len(rows)

    mm, ext, exm = (avg("market_maker_round_trip_bps"),
                    avg("exchange_round_trip_taker_bps"),
                    avg("exchange_round_trip_maker_bps"))
    best = min([("market maker", mm), ("exchange taker", ext), ("exchange maker", exm)],
               key=lambda x: x[1])
    return {
        "rows": rows, "errors": errors, "n": len(rows),
        "thirty_day_volume_usd": vol,
        "fee_tier_taker_pct": taker_pct, "fee_tier_maker_pct": maker_pct,
        "average_market_maker_round_trip_bps": mm,
        "average_exchange_taker_round_trip_bps": ext,
        "average_exchange_maker_round_trip_bps": exm,
        "cheapest_route": best[0], "cheapest_round_trip_bps": best[1],
        "verdict": (
            f"Cheapest route right now is {best[0]} at {best[1]:.0f} bps a round "
            f"trip. The measured cross-sectional momentum edge is about 61 bps, so "
            + ("this CLEARS the toll." if best[1] < 61 else
               f"the edge is still {best[1]/61:.1f}x short.")),
        "caveat": ("Exchange (v2) orders through the API are charged the TAKER rate "
                   "until Robinhood finishes rolling out maker/taker, so the maker "
                   "column is what would be possible, not what you would pay today."),
    }
