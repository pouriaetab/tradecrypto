"""The live loop: build data -> generate signals -> risk-check -> size -> execute
-> manage positions -> mark equity -> update beliefs.

Runs in a background thread. Every branch writes to the database, so the
dashboard is a view of what actually happened rather than a re-derivation.
"""
from __future__ import annotations

import json
import threading
import time

import numpy as np

from app.config import get_settings
from app.core import db, journal, liveness, vault
from app.strategy.dynamic_target import dynamic_target
from app.strategy import time_budget
from app.research import trade_plan
from app.core import mode as mode_mod
from app.data import attention as attention_mod
from app.data import selection
from app.data import universe
from app.data.feeds import Quote, cross_check, get_fallback_feed, get_feed
from app.execution import budget, cost_model, desk, symbol_cost
from app.execution.broker import OrderRequest, get_broker
from app.feedback import loop as feedback
from app.research import breakout as breakout_mod
from app.risk import guards
from app.strategy.base import Panel
from app.strategy import base as _base
from app.strategy.registry import build as build_strategy

# Rebuilding a 50-coin x 5000-bar panel every 15 seconds reads a quarter of a
# million rows and allocates fresh numpy matrices each time. The live loop needs
# recency, not depth: the deepest warmup any strategy declares is about 1080
# bars, so 1500 is ample and roughly three times cheaper.
LIVE_PANEL_BARS = 1500
PANEL_TTL_S = 45.0
COST_TABLE_TTL_S = 900.0

_cache = {"panel": None, "panel_at": 0.0, "obs": None, "coef": None, "cost_at": 0.0}


def _live_panel() -> Panel:
    now = time.time()
    if _cache["panel"] is not None and (now - _cache["panel_at"]) < PANEL_TTL_S:
        return _cache["panel"]
    p = build_panel(limit=LIVE_PANEL_BARS)
    _cache["panel"], _cache["panel_at"] = p, now
    return p


_TF_TTL_S = 300.0
_tf_cache: dict[int, tuple[float, "Panel"]] = {}

# How often each timeframe's bars are actually pulled from the feed.
#
# The old arrangement had this exactly backwards. The tick fetched 1-minute
# candles for all 75 coins every 45 seconds, while the HOURLY bars -- the ones
# the live roster reads -- were refreshed only by history_topup, every four
# hours. So an hourly strategy hunting a coin building at 1am was looking at
# bars that could be from 9pm. It could not have seen the move it was written
# for. Each timeframe now refreshes on a cadence that suits it.
_REFRESH_TTL_S = {60: 60.0, 300: 150.0, 900: 300.0,
                  3600: 600.0, 21600: 1800.0, 86400: 3600.0}
_refreshed_at: dict[int, float] = {}

# The newest mid seen for each symbol this process has quoted. manage_positions
# quotes every open position at the top of every tick, so by the time the book
# is marked these are seconds old -- fresher than any stored bar.
_last_mid: dict[str, float] = {}


def _panel_for(bar_seconds: int, bars: int = 4000, refresh: bool = False) -> "Panel":
    """A panel at a specific bar size, cached, refreshed on its own cadence."""
    now = time.time()
    hit = _tf_cache.get(bar_seconds)
    if hit and (now - hit[0]) < _TF_TTL_S:
        return hit[1]
    do_fetch = False
    if refresh:
        ttl = _REFRESH_TTL_S.get(int(bar_seconds), 600.0)
        do_fetch = (now - _refreshed_at.get(int(bar_seconds), 0.0)) >= ttl
    # build_panel stamps _refreshed_at itself, and only when the fetch actually
    # brought something back. A refresh that failed for every coin is not a
    # refresh, and must not buy another full TTL of silence.
    p = build_panel(granularity=bar_seconds, limit=bars, refresh=do_fetch)
    _tf_cache[int(bar_seconds)] = (now, p)
    return p


def _cost_tables() -> tuple[dict, dict]:
    """Per-coin cost inputs change on the timescale of hours, not seconds."""
    now = time.time()
    if _cache["obs"] is None or (now - _cache["cost_at"]) > COST_TABLE_TTL_S:
        _cache["obs"] = symbol_cost.observables()
        _cache["coef"] = symbol_cost.fit_coefficients()
        _cache["cost_at"] = now
    return _cache["obs"], _cache["coef"]


_state = {
    "running": False,
    "thread": None,
    "last_tick": None,
    "last_error": None,
    "ticks": 0,
    # There were TWO rosters. This list, and ACTIVE_STRATEGIES in
    # strategy/registry.py. Retiring fast_flip in the registry did nothing
    # because the engine read this copy instead -- which is why fast_flip was
    # still booking 18-second scalps hours after it was retired. One source of
    # truth: the registry.
    "strategies": None,
}


def _default_strategies() -> list[str]:
    from app.strategy.registry import ACTIVE_STRATEGIES
    # A strategy the evidence has condemned is skipped here rather than edited out
    # of the registry, so the daily relearn pass can reverse it without a deploy.
    try:
        from app.feedback import relearn
        off = relearn.paused()
    except Exception:
        off = set()
    return [s for s in ACTIVE_STRATEGIES if s not in off]
_lock = threading.Lock()


# ── data ──────────────────────────────────────────────────────────────────────
def store_bars(symbol: str, product: str, source: str, granularity: int, bars) -> None:
    db.executemany(
        """INSERT OR REPLACE INTO bars(symbol, ts, granularity, open, high, low, close, volume, source)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [(symbol, b.ts, granularity, b.open, b.high, b.low, b.close, b.volume, source) for b in bars],
    )



def _prior_entries_today(mode: str, symbol: str) -> int:
    """How many times this coin has already been entered today, as a feature."""
    from app.core import clock
    try:
        start = clock.local_day_start()
    except Exception:
        return 0
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM orders WHERE mode=? AND symbol=? AND side='buy' "
        "AND status='filled' AND ts_filled >= ?", (mode, symbol, start))
    return int((row["n"] if row else 0) or 0)


def build_panel(symbols: list[str] | None = None, granularity: int = 60,
                limit: int = 5000, refresh: bool = True,
                fetch_limit: int = 300) -> Panel:
    """Fetch recent candles for the universe, persist them, and align a matrix.

    Persisting matters: Robinhood keeps no history for us and the public feeds
    only serve a few hundred recent candles. Every tick therefore grows a local
    history that the backtester can use later. Day one has almost no data --
    that is a fact the UI states plainly rather than papering over.
    """
    rows = universe.active_universe()
    if symbols:
        want = {s.upper() for s in symbols}
        rows = [r for r in rows if r["symbol"] in want]
    feed = get_feed()

    # Bound the history by TIME, not by rows-per-symbol.
    #
    # `limit` reads like "a panel `limit` bars tall", but the panel's height is
    # the UNION of every coin's timestamps, and a minute bar only exists where a
    # trade printed. A thin coin's last 1,500 prints span four days; BTC's span
    # one. So limit=1500 built a panel 5,621 rows tall -- 3.7x what was asked
    # for, the extra rows almost entirely holes, and every one of them carried
    # through five float64 matrices and a dict of 375,000 Python tuples, every
    # 45 seconds. Anchoring on time instead makes the height exactly what the
    # caller asked for and puts every coin on the same rows.
    cutoff = time.time() - (float(limit) + 1.0) * float(granularity)

    if refresh:
        # Retry once, and judge the refresh as a whole.
        #
        # At 00:04 one night every one of the 75 coins failed with a DNS error,
        # and 34 more failed at 03:13 with a dropped TLS handshake -- inside the
        # 1am-4am window this whole strategy exists to watch. Each failure was
        # one attempt, one log line, and then the timeframe was marked refreshed
        # anyway, so the desk sat on stale bars for the rest of the cycle. A
        # blip lasting seconds should not cost ten minutes of blindness.
        ok_n = 0
        fail_n = 0
        first_err = None

        # The fetches run a few at a time, the stores run here in order.
        # Seventy-five sequential candle calls at ~0.3 s each held the tick
        # for ~20 s on every refresh; with pump_catch reading 15-minute bars
        # that refresh now happens every 5 minutes as well as every 10. Three
        # workers keep the burst under Coinbase's ~10 req/s soft limit. HTTP
        # only in the threads -- SQLite is touched from this thread alone.
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(r):
            sym, product = r["symbol"], r["feed_product"]
            for attempt in (1, 2):
                try:
                    # Public feeds serve only a few hundred candles per call; the
                    # local store is what accumulates real history over days.
                    return sym, product, feed.candles(product, granularity=granularity,
                                                      limit=fetch_limit), None
                except Exception as exc:
                    if attempt == 1:
                        time.sleep(0.25)
                        continue
                    return sym, product, None, f"{sym}: {exc}"
            return sym, product, None, f"{sym}: unreachable"

        with ThreadPoolExecutor(max_workers=3) as ex:
            fetched = list(ex.map(_fetch, rows))
        for sym, product, got, err in fetched:
            if got is None:
                fail_n += 1
                if first_err is None:
                    first_err = err
                continue
            try:
                store_bars(sym, product, feed.name, granularity, got)
                ok_n += 1
            except Exception as exc:
                fail_n += 1
                if first_err is None:
                    first_err = f"{sym} (store): {exc}"
        # One line for the whole sweep, not 75. A per-coin line for a network
        # outage is the same fact repeated until it is unreadable.
        if fail_n:
            db.log_event(
                "WARNING", "data",
                f"{granularity}s candle refresh: {ok_n} ok, {fail_n} failed after a "
                f"retry each -- first: {first_err}")
        if ok_n:
            _refreshed_at[int(granularity)] = time.time()
        else:
            db.log_event(
                "ERROR", "data",
                f"{granularity}s candle refresh brought back NOTHING for any of "
                f"{len(rows)} coins -- the feed is unreachable. Not marking this "
                f"timeframe refreshed; the next tick will try again immediately.")

    # Load the bars straight into the matrices, a chunk at a time.
    #
    # This used to build a dict of dicts of tuples -- one Python tuple per bar
    # per coin, and db.query() wrapped every row in a dict on the way -- and
    # hold all of it before a single number reached numpy. For the 35,000-bar
    # panel the model-lab route asks for, that is 2.6 MILLION rows: several
    # hundred megabytes of interpreter objects to produce 105 MB of float.
    # Opening that page was enough on its own to get the process killed. Now
    # only the timestamp axis lives in Python and the bars stream into place.
    want_syms = sorted({r["symbol"] for r in rows})
    if not want_syms:
        return Panel(symbols=[], ts=np.array([]), close=np.zeros((0, 0)))
    place = ",".join("?" * len(want_syms))
    scope = (granularity, cutoff, *want_syms)

    tss = [r["ts"] for r in db.query(
        f"""SELECT DISTINCT ts FROM bars
            WHERE granularity=? AND ts >= ? AND symbol IN ({place})
            ORDER BY ts""", scope)]
    if len(tss) > limit:
        tss = tss[-limit:]                      # keep the most recent
    if not tss:
        return Panel(symbols=[], ts=np.array([]), close=np.zeros((0, 0)))
    floor_ts = tss[0]

    syms = [r["symbol"] for r in db.query(
        f"""SELECT DISTINCT symbol FROM bars
            WHERE granularity=? AND ts >= ? AND symbol IN ({place})
            ORDER BY symbol""", (granularity, floor_ts, *want_syms))]
    if not syms:
        return Panel(symbols=[], ts=np.array([]), close=np.zeros((0, 0)))

    T, N = len(tss), len(syms)
    o = np.full((T, N), np.nan); h = np.full((T, N), np.nan)
    lo = np.full((T, N), np.nan); c = np.full((T, N), np.nan); v = np.full((T, N), np.nan)
    tidx = {t: i for i, t in enumerate(tss)}
    sidx = {sym: j for j, sym in enumerate(syms)}

    for batch in db.stream(
            f"""SELECT symbol, ts, open, high, low, close, volume FROM bars
                WHERE granularity=? AND ts >= ? AND symbol IN ({place})""",
            (granularity, floor_ts, *want_syms)):
        for sym, ts_, b_o, b_h, b_l, b_c, b_v in batch:
            j = sidx.get(sym)
            if j is None and isinstance(sym, (bytes, bytearray)):
                j = sidx.get(sym.decode("utf-8", "ignore"))
            i = tidx.get(ts_)
            if i is None or j is None:
                continue
            o[i, j] = b_o; h[i, j] = b_h; lo[i, j] = b_l; c[i, j] = b_c; v[i, j] = b_v
    common = tss
    # Fill missing candles. `common` is every timestamp in the window, so a coin
    # that was not listed yet, or that the exchange skipped, leaves holes.
    #
    # Filling only `close` (what this used to do) was a real and expensive bug:
    # high, low and volume stayed NaN, and every rolling statistic built on them
    # is a prefix sum, so one hole turned into NaN for the whole rest of the
    # series. BTC had 27 holes in 35,027 bars and produced ZERO breakout events.
    #
    # A missing candle means "no trade printed in this interval", so the honest
    # fill is a flat bar at the last known price with zero volume — not a
    # repeated volume, which would invent liquidity that never existed. Bars
    # before a coin's first print stay NaN: it did not exist yet.
    # Vectorised forward fill. The row-by-row version of this was T*N Python
    # iterations — 1.75 million for a 4-year, 50-coin hourly panel, on every
    # research job and every live refresh.
    if T and N:
        valid = np.isfinite(c)
        rows = np.arange(T)[:, None]
        src = np.maximum.accumulate(np.where(valid, rows, 0), axis=0)
        carried = c[src, np.arange(N)[None, :]]           # last known close
        first_valid = np.argmax(valid, axis=0)
        never = (~valid.any(axis=0))
        before = (rows < first_valid[None, :]) | never[None, :]   # not listed yet
        fillable = ~before
        gap = fillable & ~valid                            # no candle printed
        c = np.where(gap, carried, c)
        for arr in (o, h, lo):
            np.copyto(arr, carried, where=fillable & ~np.isfinite(arr))
        np.copyto(v, 0.0, where=fillable & ~np.isfinite(v))
    return Panel(symbols=syms, ts=np.array(common, dtype=float),
                 close=c, high=h, low=lo, volume=v)


# ── the live mid is Robinhood's, when Robinhood answers ──────────────────────
#
# The fill economics were already Robinhood's (the measured per-coin spread is
# charged on every paper fill), but the MID every stop, target and fill was
# judged against came from Coinbase -- one sequential HTTP call per open
# position, plus one Kraken call each to cross-check, which on 2026-09-19 was
# 54% of a 5.2 s tick (11 positions). So:
#
#   * one batched call to Robinhood's v1 best_bid_ask for every coin the tick
#     needs (10 symbols per request), cached for RH_QUOTE_TTL_S;
#   * Coinbase fetched CONCURRENTLY for the same coins (HTTP only in the
#     threads; the database is touched from the tick thread alone);
#   * the Robinhood mid is primary, Coinbase is the cross-check; if Robinhood
#     has no answer for a coin the old path (Coinbase primary, Kraken check)
#     runs unchanged, so nothing depends on Robinhood being up.
#
# What is deliberately preserved:
#   * BOTH quotes are written to `quotes`. The Coinbase row keeps `spread_bps`,
#     so symbol_cost.observables (venue tightness) sees exactly what it saw
#     before. The Robinhood row carries spread_bps=NULL and source='robinhood'
#     -- its spread is the markup we already charge, not a venue condition.
#   * price_basis excludes source='robinhood' from "our feed", so "does our
#     price match Robinhood's" cannot become a comparison of Robinhood with
#     itself -- and it now compares against a Coinbase quote seconds old
#     rather than the 235-hour-old ones it was finding for untraded coins.
RH_QUOTE_TTL_S = 12.0
_rh_quotes: dict[str, tuple[Quote, float]] = {}      # symbol -> (quote, fetched_at)
_cb_prefetch: dict[str, Quote] = {}                  # symbol -> Coinbase quote, this pass only


def _rh_quote_from_row(row: dict) -> Quote | None:
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
    if not sym or not mid or mid <= 0:
        return None
    return Quote(symbol=sym, bid=float(bid or 0.0), ask=float(ask or 0.0),
                 last=float(mid), ts=time.time(), source="robinhood")


def prefetch_quotes(symbols: list[str], products: dict[str, str]) -> dict:
    """Fill the Robinhood cache in one batched call and fetch Coinbase for the
    same coins concurrently. Returns what it managed, for the log."""
    from concurrent.futures import ThreadPoolExecutor
    want = [s for s in dict.fromkeys(symbols) if s]
    got = {"robinhood": 0, "coinbase": 0, "rh_error": None}
    if not want:
        return got
    now = time.time()
    stale = [s for s in want if (now - _rh_quotes.get(s, (None, 0.0))[1]) > RH_QUOTE_TTL_S]
    if stale:
        try:
            from app.execution import rh_api
            c = rh_api.RobinhoodCrypto()
            if c.configured:
                for i in range(0, len(stale), 10):
                    batch = stale[i:i + 10]
                    resp = c.best_bid_ask(*[f"{s}-USD" for s in batch], v2=False)
                    for row in (resp.get("results") if isinstance(resp, dict) else None) or []:
                        q = _rh_quote_from_row(row)
                        if q is not None and q.symbol in want:
                            _rh_quotes[q.symbol] = (q, time.time())
                            got["robinhood"] += 1
        except Exception as exc:
            got["rh_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    feed = get_feed()

    def _one(sym: str):
        try:
            return sym, feed.quote(products.get(sym) or f"{sym}-USD")
        except Exception:
            return sym, None
    _cb_prefetch.clear()
    with ThreadPoolExecutor(max_workers=min(8, len(want))) as ex:
        for sym, q in ex.map(_one, want):
            if q is not None:
                _cb_prefetch[sym] = q
                got["coinbase"] += 1
    return got


def live_quote(symbol: str, product: str) -> tuple[Quote, dict]:
    now = time.time()
    rh, at = _rh_quotes.get(symbol, (None, 0.0))
    rh = rh if (rh is not None and (now - at) <= RH_QUOTE_TTL_S) else None
    cb = _cb_prefetch.pop(symbol, None)
    if cb is None:
        cb = get_feed().quote(product)
    if rh is not None:
        q, agree = rh, cross_check(symbol, rh, cb)
    else:
        q = cb
        try:
            fb = get_fallback_feed()
            fq = fb.quote(fb.products().get(symbol, product))
            agree = cross_check(symbol, q, fq)
        except Exception as exc:
            agree = {"agree": False, "reason": f"cross-check unavailable: {exc}", "diff_bps": None}
    try:
        if q.mid and np.isfinite(q.mid):
            _last_mid[symbol] = float(q.mid)
    except Exception:
        pass
    # The Coinbase row, exactly as before (spread_bps = venue tightness).
    db.execute(
        "INSERT INTO quotes(ts, symbol, bid, ask, mid, last, spread_bps, source) VALUES (?,?,?,?,?,?,?,?)",
        (cb.ts, symbol, cb.bid, cb.ask, cb.mid, cb.last, cb.spread_bps, cb.source),
    )
    if rh is not None:
        # The Robinhood row: the price the book is marked and exited at.
        # spread_bps is NULL on purpose -- see the note above.
        db.execute(
            "INSERT INTO quotes(ts, symbol, bid, ask, mid, last, spread_bps, source) VALUES (?,?,?,?,?,?,?,?)",
            (rh.ts, symbol, rh.bid, rh.ask, rh.mid, rh.last, None, "robinhood"),
        )
    return q, agree


# ── position management ───────────────────────────────────────────────────────
# How the dynamic target decays when a trade stalls. Replayed on 85 closed
# paper trades before shipping; see the note at the decision site.
# Positions already reported as late, so the note fires once per position and
# not once per tick. Cleared by a restart, which is correct: a fresh process
# re-reports the book it inherits, once.
_LATE_NOTED: set[tuple] = set()

_ATR_CACHE: dict[str, tuple[float, float]] = {}
_ATR_TTL_S = 900.0          # a 24h average range does not move in 15 minutes


def hourly_range_frac(symbol: str) -> float:
    """The coin's average hourly high-low range over the last 24h, as a fraction
    of price. Cached: this is read once per position per tick and the value is
    a 24-hour average, so a fresh query every few seconds buys nothing."""
    now = time.time()
    hit = _ATR_CACHE.get(symbol)
    if hit and now - hit[0] < _ATR_TTL_S:
        return hit[1]
    val = 0.0
    try:
        rows = db.query(
            "SELECT high, low, close FROM bars WHERE symbol=? AND granularity=3600 "
            "AND close IS NOT NULL AND close > 0 ORDER BY ts DESC LIMIT 24", (symbol,))
        rng = [(float(r["high"]) - float(r["low"])) / float(r["close"])
               for r in rows if r["high"] and r["low"] and r["close"]]
        if rng:
            val = sum(rng) / len(rng)
    except Exception:
        val = 0.0
    _ATR_CACHE[symbol] = (now, val)
    return val


# Best of the seven-arm family on 136 replayed trades (2026-09-23):
#   dyn_lead3_decay_fast  -0.704%  win 13%   <- this setting
#   dyn_decay_only        -0.738%  win  4%
#   dyn_control           -0.741%  win  1%   (fixed target, no decay)
# Starting at 4h and falling twice as fast beat the 8h/0.4%/h arm on both
# mean and win rate, so the live constants are the measured ones.
# Close a trade that failed its own prediction as soon as it is green again.
# Measured before shipping; see the note at the decision site. One constant to
# turn it off.
EXIT_SEEKING_CLOSES = True

# Close a trade that broke its expected dip and then climbed back to +0.50%.
# De-risking, not profit: same mean, 26% less volatility, big losses cut from
# 24% of trades to 14%. See the note in research/trade_plan.py.
# OFF. Measured 2026-09-23: the recovery exit and the target decay do the same
# job -- both take a small profit early -- and running BOTH takes it twice as
# often and costs money. With the decay on, adding recovery moved the mean from
# +0.072% to -0.361% while the risk numbers barely changed (sd 3.91 -> 3.61,
# big losses 14% -> 14%). The decay alone already catches the operator's own
# examples: OP's target would have decayed below +1.24% by hour 8.2 and sold.
#
# The code and the `recovered` state stay, because seeing the state on the page
# is useful even when it does not trade. Set True to trade it instead.
RECOVERY_CLOSES = False

# WHEN THE TARGET STARTS COMING DOWN, as a fraction of the MEDIAN hour that
# targets of this shape arrive by -- not the full window.
#
# 2026-09-23, the operator: UNI peaked at +4.9% while the system held out for a
# 9.9% target that arrives 48% of the time. The decay did not start until hour
# 24, so a good gain went unclaimed waiting for a number that probably was not
# coming.
#
# Swept on 59 closed trades. The MEAN column is noisy and not monotone -- the
# best cell (0.75x, +0.280%) sits between two worse ones, so it is not trusted.
# The RISK columns are perfectly monotone and are what this is set on:
#
#   decay starts at   volatility   trades losing >5%
#   never                 4.83%          20%
#   1.5x median           4.75%          20%
#   1.0x median           4.42%          17%
#   0.75x median          4.09%          14%
#   0.5x median           3.91%          14%     <- here
#   0.25x median          3.65%          14%
#
# 0.5x is chosen over the best-scoring cell deliberately: it is inside the
# smooth part of the curve rather than on its peak, its mean (+0.072%) is no
# worse than today's (-0.005%), and it takes the full drop in big losses.
# The odds below which this trade's target stops being worth waiting for.
# Swept on 59 closed trades; 0.40 sits between the two neighbours rather than on
# the best-scoring cell (0.35, +0.253%), which is a spike, not a shape.
MIN_ODDS_TO_HOLD_TARGET = 0.40

# How fast the target comes down once the odds have gone. Per HOUR since the
# odds first broke -- normalised on time, NOT accumulated per tick: the engine
# ticks every few seconds, so a per-tick accumulator would decay roughly sixty
# times too fast in production while looking correct in an hourly backtest.
TARGET_SHRINK_PER_H = 0.008

TARGET_DECAY_AT_MEDIAN_K = 0.5

TARGET_DECAY_AFTER_H = 4.0
TARGET_DECAY_PER_H = 0.008


def _range_frac_as_of(symbol: str, ts: float) -> float:
    """The coin's average hourly range over the 24h BEFORE a moment. Used only
    to reconstruct a plan for a position that predates the table; the live path
    uses hourly_range_frac(), which is the same thing as of now."""
    try:
        rows = db.query(
            "SELECT high, low, close FROM bars WHERE symbol=? AND granularity=3600 "
            "AND ts < ? AND close IS NOT NULL AND close > 0 ORDER BY ts DESC LIMIT 24",
            (symbol, float(ts)))
        rng = [(float(r["high"]) - float(r["low"])) / float(r["close"])
               for r in rows if r["high"] and r["low"] and r["close"]]
        return sum(rng) / len(rng) if rng else 0.0
    except Exception:
        return 0.0


def _sluggish_shrink(pos: dict, odds: float, mode: str) -> float:
    """How much of the target to give up, given how long the odds have been bad.

    The moment the odds first fall below the bar is written to the row and never
    cleared -- a one-way door, because a trade that has broken once has shown
    its character, and because a target that can come BACK is not a target you
    lowered, it is one you delayed.

    Measured against the clock it replaces (59 closed trades): mean -0.074% vs
    +0.033%, volatility 3.90% vs 3.89%, trades losing more than 5% 14% vs 14%.
    Indistinguishable on this sample -- chosen on structure, not on that noise.
    """
    if odds >= MIN_ODDS_TO_HOLD_TARGET:
        since = pos.get("sluggish_since")
        if not since:
            return 0.0
    else:
        since = pos.get("sluggish_since")
        if not since:
            since = time.time()
            try:
                db.execute("UPDATE positions SET sluggish_since=?, sluggish_odds=? "
                           "WHERE symbol=? AND mode=? AND strategy=?",
                           (since, float(odds), pos["symbol"], mode, pos["strategy"]))
                pos["sluggish_since"] = since
                pos["sluggish_odds"] = float(odds)
                # THE PENALTY FEEDBACK. Written the moment the prediction turns,
                # with everything needed to ask later why this was promoted.
                journal.append("prediction_turned", {
                    "symbol": pos["symbol"], "strategy": pos["strategy"], "mode": mode,
                    "odds_now": round(float(odds), 3),
                    "bar": MIN_ODDS_TO_HOLD_TARGET,
                    "age_h": round((since - float(pos["opened_ts"])) / 3600.0, 2),
                    "entry_px": float(pos["avg_px"]),
                })
                db.log_event("INFO", "execution",
                             f"{pos['symbol']} {pos['strategy']}: odds of reaching its "
                             f"target fell to {odds*100:.0f}% (bar {MIN_ODDS_TO_HOLD_TARGET*100:.0f}%). "
                             f"The target now comes down toward breakeven.")
            except Exception:
                return 0.0
    try:
        hours = max(0.0, (time.time() - float(since)) / 3600.0)
    except Exception:
        return 0.0
    return TARGET_SHRINK_PER_H * hours


def _grade_plan(pos: dict, q, entry: float, age_h: float, mode: str) -> None:
    """Score the plan against what actually happened, and record a state change.

    peak_px on the row is the high-water mark the ratchet already maintains, so
    'best' costs nothing. 'worst' is not tracked on the row, so it is derived
    from the current price and only ever moves down -- which is the honest
    reading: we know the trade is at least this far down.
    """
    try:
        plan = db.query_one(
            "SELECT * FROM trade_plans WHERE symbol=? AND strategy=? AND mode=? "
            "AND opened_ts=?",
            (pos["symbol"], pos["strategy"], mode, float(pos["opened_ts"])))
        if not plan:
            # A position that predates the plan table, or one whose plan could
            # not be written at entry. Reconstruct it from the bars that existed
            # BEFORE the fill -- never from today's range, which would be
            # hindsight wearing a prediction's clothes -- and label it so the
            # page never presents it as something we actually wrote down.
            _atr = _range_frac_as_of(pos["symbol"], float(pos["opened_ts"]))
            _p = trade_plan.build(
                pos["symbol"], pos["strategy"], entry,
                pos.get("target_px"), pos.get("stop_px"), _atr,
                why={"reconstructed": True,
                     "note": "this position opened before plans were recorded; "
                             "rebuilt from the bars available at entry"})
            trade_plan.record(_p, mode, float(pos["opened_ts"]))
            plan = db.query_one(
                "SELECT * FROM trade_plans WHERE symbol=? AND strategy=? AND mode=? "
                "AND opened_ts=?",
                (pos["symbol"], pos["strategy"], mode, float(pos["opened_ts"])))
            if not plan:
                return
        side = 0.0
        try:
            from app.execution import rh_spread as _rs
            side = float(_rs.get(pos["symbol"])["spread_pct"]) / 100.0
        except Exception:
            side = 0.0095
        now_pct = (float(q.mid) * (1 - side) / entry - 1.0) * 100.0
        best_pct = ((float(pos["peak_px"]) * (1 - side) / entry - 1.0) * 100.0
                    if pos.get("peak_px") else now_pct)
        worst_pct = min(float(plan["worst_pct"] if plan["worst_pct"] is not None
                              else now_pct), now_pct)
        reached = bool(plan["target_px"] and float(q.mid) >= float(plan["target_px"]))
        state, note = trade_plan.grade(dict(plan), age_h, worst_pct, best_pct,
                                       now_pct, reached)
        # SELL A FAILED TRADE THE MOMENT IT IS GREEN.
        #
        # 2026-09-23, the operator: "there was a brief opportunity to get out and
        # close and move on". Replayed on every closed paper trade with a
        # recoverable target: 19 failed their window, 5 later showed a green
        # moment, 4 of those 5 were better off taking it. +0.34 pts/trade.
        #
        # Note the SIGN is opposite to the broad version of this idea. "Past 20h
        # and green, take it" measured -3 pts/trade (session 66) because it
        # capped winners too. This one can only fire on a trade that has already
        # failed its own prediction, so it never touches a trade that is working.
        # That distinction is the whole rule.
        if state == "recovered" and RECOVERY_CLOSES:
            pos["_plan_exit"] = note
        elif state == "exit_seeking" and EXIT_SEEKING_CLOSES and now_pct > 0:
            pos["_plan_exit"] = note
        if trade_plan.mark(pos["symbol"], pos["strategy"], mode,
                           float(pos["opened_ts"]), state, note, worst_pct, best_pct):
            db.log_event("INFO", "execution",
                         f"{pos['symbol']} {pos['strategy']} plan -> {state.upper()}: {note}")
            journal.append("plan_state", {
                "symbol": pos["symbol"], "strategy": pos["strategy"], "mode": mode,
                "state": state, "note": note, "age_h": round(age_h, 2),
                "now_pct": round(now_pct, 3), "worst_pct": round(worst_pct, 3)})
    except Exception:
        pass            # grading is observation; it must never break execution


def _note_late_trade(pos: dict, bud: dict, age_h: float, mode: str) -> None:
    """Record, once per position, that a trade missed its predicted window.

    This is the feedback the operator asked for: "the strategy was wrong from
    the get go and we feed it back to the model to penalize it". The row is
    written where the entry-quality research already looks, so a strategy that
    keeps producing late trades is visible as a rate, not as a feeling.
    """
    try:
        db.log_event("INFO", "execution",
                     f"{pos['symbol']} {pos['strategy']} is LATE: {age_h:.1f}h against a "
                     f"{bud['p70_h']}h window ({bud['why']}, {bud['p_reach']*100:.0f}% of "
                     f"these ever arrive). Now seeking the best exit, not the target.")
        journal.append("trade_late", {
            "symbol": pos["symbol"], "strategy": pos["strategy"], "mode": mode,
            "age_h": round(age_h, 2), "window_h": bud["p70_h"],
            "ratio": bud["ratio"], "p_reach": bud["p_reach"],
            "entry_px": float(pos["avg_px"]),
        })
        liveness.fired("trade_late", f"{pos['symbol']} {age_h:.1f}h > {bud['p70_h']}h")
    except Exception:
        pass            # feedback must never be able to stop a trade closing


def covered_px(symbol: str, entry: float) -> float:
    """The price at which selling returns the money paid, both spreads in."""
    try:
        from app.execution import rh_spread as _rs
        side = float(_rs.get(symbol)["spread_pct"]) / 100.0
    except Exception:
        side = 0.0095
    return entry / (1.0 - side) if side < 1.0 else entry


def _close_position(pos: dict, quote: Quote, reason: str, mode: str) -> None:
    # In advisory mode the close is a request to a human, not an event. Do not
    # queue a second ticket while one is still outstanding, and do not book a
    # trade until a real fill price comes back.
    if mode == "advisory" and desk.has_pending_close(pos["symbol"], mode):
        return

    broker = get_broker(mode)
    side = "sell" if pos["qty"] > 0 else "buy"
    notional = abs(pos["qty"]) * quote.mid
    req = OrderRequest(symbol=pos["symbol"], side=side, notional_usd=notional,
                       strategy=pos["strategy"], intent="close",
                       mid_at_decision=quote.mid, metadata={"exit_reason": reason})
    fill = broker.place(req, quote.mid)
    if fill.status == "pending":
        db.log_event("INFO", "desk",
                     f"EXIT ticket posted for {pos['symbol']} ({reason}) -- awaiting your fill")
        return
    if fill.status != "filled":
        db.log_event("WARNING", "execution", f"close of {pos['symbol']} not filled ({fill.status})")
        return

    qty = abs(pos["qty"])
    entry, exit_px = pos["avg_px"], fill.price
    sgn = 1 if pos["qty"] > 0 else -1
    # The spread was being charged TWICE.
    #
    # `entry` is the fill price and `exit_px` is the fill price, so fill-to-fill
    # ALREADY has both sides of the spread inside it. That figure was booked as
    # `gross`, and then the spread was computed again and subtracted from it. On
    # 2026-09-12 the two closed trades really lost $3.44 fill-to-fill and were
    # recorded as -$8.89 -- a 2.6x overstatement, on the exact number that
    # decides whether a strategy is working.
    #
    # The honest decomposition, which is also what the operator asked to see:
    #     gross = mid to mid      what the coin did, before we paid anyone
    #     cost  = spread, BOTH sides, at the mids we were quoted
    #     net   = gross - cost    exactly the fill-to-fill result, no double count
    _open = db.query_one(
        "SELECT mid_at_submit, mid_at_decision FROM orders "
        "WHERE symbol=? AND mode=? AND strategy=? AND intent='open' AND status='filled' "
        "ORDER BY ts_decided DESC LIMIT 1", (pos["symbol"], mode, pos["strategy"]))
    entry_mid = None
    if _open:
        entry_mid = _open["mid_at_submit"] or _open["mid_at_decision"]
    entry_mid = float(entry_mid) if entry_mid else entry
    exit_mid = quote.mid if quote.mid else exit_px
    gross = sgn * (exit_mid - entry_mid) * qty
    entry_cost = abs(entry - entry_mid) * qty
    cost = abs(exit_px - exit_mid) * qty + entry_cost
    net = sgn * (exit_px - entry) * qty          # fill to fill: the real outcome
    # gross - cost and fill-to-fill must agree. If a broker ever fills us better
    # than the quote they are allowed to differ slightly; anything larger is a
    # bookkeeping error and should be loud rather than silently booked.
    if abs((gross - cost) - net) > max(0.01, 0.002 * abs(notional)):
        db.log_event("WARNING", "execution",
                     f"P&L decomposition disagrees for {pos['symbol']}: "
                     f"gross {gross:.4f} - cost {cost:.4f} != fill-to-fill {net:.4f}")
    _cid = db.query_one("SELECT id FROM orders WHERE client_id=?", (fill.client_id,))
    close_order_id = _cid["id"] if _cid else None
    trade_id = db.execute(
        """INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close,
                              holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, predicted_edge_bps,
                              open_order_id, close_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pos["symbol"], pos["strategy"], mode, pos["qty"], entry, exit_px, pos["opened_ts"],
         time.time(), time.time() - pos["opened_ts"], gross, cost, net, None,
         pos.get("open_order_id"), close_order_id),
    )
    # Which rule made this trade -- class version + hash of the parameters in
    # force. Without it nothing can compare versions or discount an old one.
    try:
        from app.research import versions as _versions
        _versions.tag_trade(trade_id, pos["strategy"])
    except Exception as exc:
        db.log_event("WARNING", "versions", f"could not tag trade {trade_id}: {exc}")
    # Also to the append-only journal, which the database cannot destroy. This
    # is the copy that would have existed on 2026-09-18, when a day of trades
    # lived in exactly one place and that place was overwritten.
    liveness.fired("trade_closed", f"{pos['symbol']} net {net:+.2f}")
    journal.append("trade_closed", {
        "trade_id": trade_id, "symbol": pos["symbol"], "strategy": pos["strategy"],
        "mode": mode, "qty": pos["qty"], "entry_px": entry, "exit_px": exit_px,
        "ts_open": pos["opened_ts"], "ts_close": time.time(),
        "gross_usd": gross, "cost_usd": cost, "net_usd": net,
        "open_order_id": pos.get("open_order_id"), "close_order_id": close_order_id,
    })
    # And out of the project entirely. See core/vault.py.
    vault.record("trade", f"trade:{trade_id}", {
        "trade_id": trade_id, "symbol": pos["symbol"], "strategy": pos["strategy"],
        "mode": mode, "qty": pos["qty"], "entry_px": entry, "exit_px": exit_px,
        "ts_open": pos["opened_ts"], "ts_close": time.time(),
        "gross_usd": gross, "cost_usd": cost, "net_usd": net,
        "open_order_id": pos.get("open_order_id"), "close_order_id": close_order_id,
        "reason": reason,
    })
    db.execute("DELETE FROM positions WHERE symbol=? AND mode=? AND strategy=?",
               (pos["symbol"], mode, pos["strategy"]))
    feedback.attribute_trade(trade_id)
    feedback.update_posterior(pos["strategy"], mode)
    db.log_event("INFO", "execution",
                 f"closed {pos['symbol']} ({reason}) net ${net:.2f} "
                 f"(coin moved ${gross:+.2f}, spread took ${cost:.2f})")


def manage_positions(mode: str) -> list[dict]:
    actions = []
    positions = guards.open_positions(mode)
    products = {r["symbol"]: r["feed_product"] for r in db.query(
        "SELECT symbol, feed_product FROM universe WHERE symbol IN (%s)"
        % ",".join("?" * len(positions)), tuple(p["symbol"] for p in positions))} if positions else {}
    try:
        prefetch_quotes([p["symbol"] for p in positions], products)
    except Exception as exc:
        db.log_event("WARNING", "data", f"quote prefetch failed, falling back per coin: {exc}")
    for pos in positions:
        row = {"feed_product": products.get(pos["symbol"])}
        if not row["feed_product"]:
            continue
        try:
            q, _ = live_quote(pos["symbol"], row["feed_product"])
        except Exception as exc:
            db.log_event("WARNING", "data", f"quote failed for {pos['symbol']}: {exc}")
            continue
        sgn = 1 if pos["qty"] > 0 else -1
        # Trailing stop. The high-water mark is stored, so the ratchet survives a
        # restart instead of resetting to the fill price and giving back the run.
        # The stop only ever moves in our favour: a quote that ticks down cannot
        # loosen it.
        if pos.get("trail_bps"):
            peak = pos.get("peak_px") or pos["avg_px"]
            new_peak = max(peak, q.mid) if sgn > 0 else min(peak, q.mid)
            trail = float(pos["trail_bps"]) / 1e4
            trailed = new_peak * (1 - sgn * trail)
            fixed = pos["stop_px"] or trailed
            best = max(fixed, trailed) if sgn > 0 else min(fixed, trailed)

            # BREAKEVEN RATCHET. Once the coin has risen far enough that selling
            # would cover both spreads, the stop never goes back below that
            # price. A trade that has already paid for itself cannot be handed
            # back.
            #
            # This exists because the operator put the objection plainly: a pure
            # trailing stop exits on a decline BY CONSTRUCTION, so it gives back
            # the trail distance from the peak every time. On the live ARB
            # position that meant the price had to rise 9.74% from the fill
            # before the exit could even be green. With this, it has to rise
            # about 1.4% — after which the trade cannot lose.
            #
            # Measured, not assumed: 260 pump entries over four years of hourly
            # bars, chronological split, held-out quarter read once. Adding this
            # improved EVERY trailing variant tested — 5 of 5, at widths from 5%
            # to 15%, fixed and volatility-scaled. The current rule was the worst
            # of the family at -2.97% a trade; with the ratchet it is -0.76%.
            # A consistent direction across every variant is worth more than any
            # one variant's point estimate.
            try:
                from app.execution import rh_spread as _rs
                side = float(_rs.get(pos["symbol"])["spread_pct"]) / 100.0
            except Exception:
                side = 0.0095
            locked_now = False
            if sgn > 0 and side < 1.0:
                covered = float(pos["avg_px"]) / (1.0 - side)   # sale here nets the fill
                if new_peak >= covered * 1.005:                 # clear of it, not on it
                    locked_now = float(pos["stop_px"] or 0.0) < covered
                    best = max(best, covered)
            if new_peak != peak or best != pos["stop_px"]:
                db.execute(
                    "UPDATE positions SET peak_px=?, stop_px=?, "
                    "stop_moves=COALESCE(stop_moves,0)+? "
                    "WHERE symbol=? AND mode=? AND strategy=?",
                    # Count only a real RAISE of the stop. A new peak with the
                    # stop unchanged is the price moving, not the stop moving,
                    # and a counter that ticks on both would say "trailing" for
                    # a stop that has never once followed anything.
                    (new_peak, best, 1 if best != pos["stop_px"] else 0,
                     pos["symbol"], mode, pos["strategy"]))
                if best != pos["stop_px"]:
                    # The claim "the trailing stop has moved" is now a COUNTER,
                    # not a reading of this file. See core/liveness.py.
                    liveness.fired("stop_ratchet",
                                   f"{pos['symbol']} {pos['stop_px']:.6g} -> {best:.6g}")
                pos["peak_px"], pos["stop_px"] = new_peak, best
                if locked_now:
                    # THE MOMENT THE PROMISE BECOMES TRUE, WRITTEN DOWN.
                    #
                    # On 2026-09-17 the operator was told a live ARB position
                    # "stops being able to lose" once the ratchet engaged. It was
                    # true when it was said and there is now no way to check it:
                    # the position row died with the database, so the claim
                    # survives only as a sentence in a chat log.
                    #
                    # A forward-looking statement about real money has to leave a
                    # record at the instant it becomes true, in the one place the
                    # database cannot take with it.
                    liveness.fired("stop_locked_breakeven", pos["symbol"])
                    journal.append("stop_locked_breakeven", {
                        "symbol": pos["symbol"], "strategy": pos["strategy"],
                        "mode": mode, "entry_px": float(pos["avg_px"]),
                        "peak_px": new_peak, "stop_px": best,
                        "covers_both_spreads_at": covered,
                        "half_spread_pct": side * 100.0,
                        "qty": float(pos.get("qty") or 0.0),
                    })
                    db.log_event("INFO", "execution",
                                 f"{pos['symbol']} stop ratcheted to breakeven "
                                 f"{best:.6f} (peak {new_peak:.6f}) — this trade can "
                                 f"no longer close at a loss")
        # DYNAMIC TARGET (2026-09-23). The operator: "I dont like the no target
        # ... analyze the circumstances at various intervals and change the
        # target accordingly to maximize profit and minimize loss."
        #
        # Only the LOWERING half is live. The other half -- holding the target
        # above the price while the coin climbs -- was replayed on all 85 closed
        # paper trades and changed the outcome of exactly ZERO of them, so it is
        # not in production. The decay changed 10, helped 9, hurt 1, +0.076
        # pts/trade. Small, and the right sign.
        #
        # target_px in the row is never mutated: the decay is computed from the
        # ORIGINAL each tick, so it cannot compound, and a restart cannot leave a
        # position with a permanently shrunken target.
        eff_target = pos["target_px"]
        if eff_target and sgn > 0 and pos.get("opened_ts"):
            try:
                _age_h = (time.time() - float(pos["opened_ts"])) / 3600.0
                _entry = float(pos["avg_px"])
                _base = float(eff_target) / _entry - 1.0
                # WHEN THE TRADE IS LATE BY ITS OWN MODEL'S STANDARD.
                #
                # 2026-09-23, the operator: "the clock for all it doesnt have to
                # be hard coded ... if position within certain predicted interval
                # didnt reach the point the algorithm predicted that it would
                # then to consider it as not a good trade."
                #
                # So the decay no longer starts at a fixed hour. It starts at the
                # hour by which 70% of targets of this SIZE on a coin of this
                # RANGE have already arrived -- 12h for an easy target, 35h for a
                # stretched one. Measured on 283k windows and validated on a
                # 2025-26 holdout; see strategy/time_budget.py.
                _atr = hourly_range_frac(pos["symbol"])
                _bud = time_budget.budget(_base, _atr)
                _late = _age_h > _bud["p70_h"]
                if _late:
                    # Keyed on the POSITION, not on the dict: `pos` is rebuilt
                    # from the database every tick, so a flag set on it is gone
                    # by the next one and the note fires a few times a minute.
                    # Caught by seeing DOGE logged twice within one restart.
                    _k = (pos["symbol"], pos["strategy"], mode,
                          round(float(pos["opened_ts"] or 0), 3))
                    if _k not in _LATE_NOTED:
                        _LATE_NOTED.add(_k)
                        _note_late_trade(pos, _bud, _age_h, mode)
                _grade_plan(pos, q, _entry, _age_h, mode)
                # THE TARGET COMES DOWN ON THE ODDS, NOT THE CLOCK.
                #
                # 2026-09-23, the operator: "dont do the hour or timely
                # hardcoding ... as this prediction seems becoming sluggish then
                # we dynamically change our rules even if the initial target or
                # waiting time was longer."
                #
                # `still_arrives` asks, on 4.9M measured observations: given we
                # are still holding and have NOT reached the target, what is the
                # chance we still do -- given the shape, the hours so far, and
                # WHERE THE PRICE IS NOW. The last of those is what the clock
                # could not see:
                #
                #   2h in, down 5.5%   -> 29%   the clock says hold, this says go
                #   32h in, up 2.5%    -> 62%   the clock says cut, this says hold
                #
                # Hours are an INPUT to the estimate. The DECISION is the odds.
                _side_pct_now = 1.0 - (covered_px(pos["symbol"], _entry) and
                                       _entry / covered_px(pos["symbol"], _entry) or 1.0)
                _now_pct = (float(q.mid) * (1 - _side_pct_now) / _entry - 1.0) * 100.0
                _odds = time_budget.still_arrives(_base, _atr, _age_h, _now_pct)
                _shrink = _sluggish_shrink(pos, _odds, mode)
                eff_target = dynamic_target(
                    _entry, float(q.mid), covered_px(pos["symbol"], _entry),
                    _base, _age_h,
                    climbing=False,          # the lead half is a proven no-op
                    shrink=_shrink)
            except Exception:
                eff_target = pos["target_px"]      # a bad tick costs nothing

        reason = None
        if pos.get("_plan_exit"):
            reason = "plan_failed_green"
        elif pos["stop_px"] and sgn * (q.mid - pos["stop_px"]) <= 0:
            reason = "stop"
        elif eff_target and sgn * (q.mid - eff_target) >= 0:
            reason = "target"
        elif pos["max_hold_s"] and (time.time() - pos["opened_ts"]) > pos["max_hold_s"]:
            reason = "time"
        if reason:
            _close_position(pos, q, reason, mode)
            actions.append({"symbol": pos["symbol"], "action": "closed", "reason": reason})
    return actions


def close_all(mode: str, reason: str = "manual") -> list[dict]:
    """Close every open position right now, at the current quote.

    For the case where the book holds trades you no longer want to be in --
    entries that turned out to be late, or a strategy that has since been
    retired. Waiting for a stop or a time exit just pays the spread later.
    """
    out = []
    for pos in guards.open_positions(mode):
        row = db.query_one("SELECT feed_product FROM universe WHERE symbol=?", (pos["symbol"],))
        if not row:
            continue
        try:
            q, _ = live_quote(pos["symbol"], row["feed_product"])
        except Exception as exc:
            out.append({"symbol": pos["symbol"], "closed": False, "error": str(exc)[:120]})
            continue
        _close_position(pos, q, reason, mode)
        out.append({"symbol": pos["symbol"], "closed": True, "at": q.mid, "reason": reason})
    if out:
        db.log_event("INFO", "execution",
                     f"closed {sum(1 for o in out if o.get('closed'))} position(s) on request ({reason})")
    return out


# ── where a tick's time actually goes ─────────────────────────────────────────
#
# The operator asked which parts of the program are expensive. Big-O on paper
# says the tick is O(P) quote calls for P open positions, O(T x N) for the
# panel and each strategy, and O(S x P) for the risk checks on S signals -- but
# a bound is not a measurement. This records the wall time of every phase of
# every tick in a BOUNDED ring (invariant 23), and /system/status reports the
# mean, the 95th percentile and each phase's share, so "which is which" is read
# off the desk rather than argued about.
import collections as _collections

_PROFILE_N = 240                      # ~1 hour at a 15 s tick
_profile: "_collections.deque[dict]" = _collections.deque(maxlen=_PROFILE_N)
_PHASES = ("positions", "build", "panels", "strategies", "mark")


def tick_profile() -> dict:
    rows = list(_profile)
    if not rows:
        return {"n": 0, "note": "no tick has completed yet"}
    tot = sorted(r["total"] for r in rows)
    n = len(tot)
    out = {"n": n, "window": f"last {n} ticks",
           "total_mean_s": sum(tot) / n, "total_p95_s": tot[int(0.95 * (n - 1))],
           "total_max_s": tot[-1], "last_s": rows[-1]["total"],
           "phases": {}}
    for ph in _PHASES:
        v = sorted(r.get(ph, 0.0) for r in rows)
        out["phases"][ph] = {"mean_s": sum(v) / n, "p95_s": v[int(0.95 * (n - 1))],
                             "share_pct": (100.0 * sum(v) / sum(tot)) if sum(tot) else 0.0}
    out["counts"] = {"positions": rows[-1].get("n_positions"),
                     "signals": rows[-1].get("n_signals"),
                     "strategies": rows[-1].get("n_strategies")}
    out["what_each_phase_is"] = {
        "positions": "one live quote per open position + stop/target/time checks (O(P), one HTTP call each)",
        "build": "instantiate each strategy with its promoted parameters",
        "panels": "refresh + assemble the bar panels the roster reads (O(T x N) per bar size; cached PANEL_TTL_S)",
        "strategies": "calibrate + generate for every strategy, then cost hurdle, sizing and risk checks per signal (O(S x P) + one 20k-row drawdown read per signal)",
        "mark": "write the equity-curve row",
    }
    return out


# ── one iteration ─────────────────────────────────────────────────────────────
def tick(strategies: list[str] | None = None, mode: str | None = None) -> dict:
    s = get_settings()
    mode = mode or mode_mod.get_mode()
    if not _state.get("strategies"):
        _state["strategies"] = _default_strategies()
    out: dict = {"ts": time.time(), "mode": mode, "signals": [], "orders": [], "closed": []}
    _t0 = time.perf_counter()
    _prof: dict = {}

    def _lap(phase: str, since: float) -> float:
        now = time.perf_counter()
        _prof[phase] = _prof.get(phase, 0.0) + (now - since)
        return now

    def _done(n_sig: int = 0, n_strat: int = 0) -> None:
        _prof["total"] = time.perf_counter() - _t0
        _prof["n_signals"] = n_sig
        _prof["n_strategies"] = n_strat
        _profile.append(dict(_prof))

    _prof["n_positions"] = len(db.query("SELECT symbol FROM positions WHERE mode=? AND qty != 0", (mode,)))
    out["closed"] = manage_positions(mode)
    _t = _lap("positions", _t0)

    # Build panels ONLY at the bar sizes something on the roster actually reads,
    # and only as deep as the deepest warm-up needs.
    #
    # The tick used to build a 1-minute panel unconditionally: 75 feed calls and
    # five 5,621 x 75 float matrices, every 45 seconds. With an hourly roster
    # nothing read it. It was the largest allocation in the process, it was
    # thrown away untouched, and macOS killed the backend for it fifteen times.
    names = list(strategies or _state["strategies"])
    built: list[tuple[str, object]] = []
    for name in names:
        try:
            # Build with the PROMOTED parameters, not the class defaults. A
            # challenger that beat the incumbent on held-out bars is only a
            # promotion if the desk then actually trades it; before this line
            # existed, retraining could have concluded anything and changed
            # nothing. active_params() returns {} until something is promoted,
            # so the default path is unchanged.
            from app.research import retrain as _retrain
            try:
                overrides = _retrain.active_params(name)
            except Exception:
                overrides = {}
            built.append((name, build_strategy(name, **overrides)))
        except Exception as exc:
            out["signals"].append({"strategy": name, "skipped": True,
                                   "reason": f"could not be built: {type(exc).__name__}: {exc}"})
    _t = _lap("build", _t)
    if not built:
        _mark_equity(mode)
        _lap("mark", _t); _done(0, 0)
        return out

    wanted: dict[int, int] = {}
    for _n, st in built:
        bs = int(getattr(st, "bar_seconds", 60) or 60)
        wanted[bs] = max(wanted.get(bs, 0), int(st.warmup_bars()) + 120)
    panels = {bs: _panel_for(bs, bars=max(500, min(need_bars, 4000)), refresh=True)
              for bs, need_bars in wanted.items()}
    _t = _lap("panels", _t)

    if not any(pp.T for pp in panels.values()):
        out["note"] = ("no bars collected yet at the sizes the running strategies "
                       "read; they stay idle until there is history to warm up on")
        _mark_equity(mode)
        _lap("mark", _t); _done(0, len(built))
        return out

    for name, strat in built:
        # Hand this strategy the bar size its parameters were written for.
        want = int(getattr(strat, "bar_seconds", 60) or 60)
        spanel = panels.get(want) or _panel_for(want, refresh=True)
        need = strat.warmup_bars() + 30
        if spanel.T < need:
            out["signals"].append({
                "strategy": name, "skipped": True,
                "reason": (f"needs {need} bars of {want}s data to warm up, "
                           f"has {spanel.T}")})
            continue
        strat.calibrate(spanel, spanel.T - 1)
        sigs = strat.generate(spanel, spanel.T - 1)
        # Recorded, not enforced: how crowded this bar was and where this signal
        # ranked in it. Nine alts climbing in the same minute is one market
        # move, and whether the crowd predicts the outcome is for the
        # retrainer to find out from these two numbers -- not for a cap.
        # And the day's weather (research/regime_days.py): the regime name and
        # the size multiplier it earned, on every signal, so the lab can judge
        # the router by the same trades it judges everything else by.
        try:
            from app.research import regime_days as _regime
            _rm = _regime.multiplier(strat.name) if sigs else {"multiplier": 1.0, "regime": None}
        except Exception:
            _rm = {"multiplier": 1.0, "regime": None}
        for _i, _sg in enumerate(sigs):
            try:
                _sg.features["burst_n"] = float(len(sigs))
                _sg.features["burst_rank"] = float(_i + 1)
                _sg.features["regime"] = _rm.get("regime")
                _sg.features["regime_mult"] = float(_rm.get("multiplier", 1.0))
            except Exception:
                pass
        if sigs:
            try:
                _syms = [x.symbol for x in sigs]
                _prod = {r["symbol"]: r["feed_product"] for r in db.query(
                    "SELECT symbol, feed_product FROM universe WHERE symbol IN (%s)"
                    % ",".join("?" * len(_syms)), tuple(_syms))}
                prefetch_quotes(_syms, _prod)
            except Exception as exc:
                db.log_event("WARNING", "data", f"quote prefetch for signals failed: {exc}")
        # The hurdle is PER COIN. Judging a BTC signal against the pessimistic
        # global prior (240 bps) when BTC's own estimated round trip is nearer
        # 80 bps rejects trades that were actually viable, and is the difference
        # between a strategy that never fires and one that does.
        obs_cache, coef_cache = _cost_tables()
        # The panel spans every TRACKED coin, because breadth and the leader
        # reading need the majors present. Only coins the selection model rates
        # core or tradeable may actually be traded.
        allowed = set(selection.tradeable_symbols())

        for sig in sigs:
            # ONE DECISION PER BAR. The engine ticks every ~16 seconds but the
            # strategies run on hourly bars, so the same bar re-emits the same
            # signal about 225 times an hour. Every one of those was written to
            # `signals` and, when it failed a guard, logged as a fresh block:
            # 157 identical "order blocked for VVV" entries in two hours, which
            # buries anything real in the log and inflates every count taken from
            # that table. A bar gets one recorded decision; a genuinely new bar
            # gets a new one.
            if db.query_one(
                    "SELECT 1 FROM signals WHERE strategy=? AND symbol=? AND ts=? LIMIT 1",
                    (strat.name, sig.symbol, sig.ts)):
                continue
            row = db.query_one("SELECT * FROM universe WHERE symbol=?", (sig.symbol,))
            if not row:
                continue
            if sig.symbol not in allowed:
                out["signals"].append({**sig.to_dict(), "decision": "rejected",
                                       "reject_reason": (
                                           f"{sig.symbol} is tracked for context but not in the "
                                           f"tradeable set (role: {row['role'] or 'watch'})")})
                continue
            try:
                q, agree = live_quote(sig.symbol, row["feed_product"])
            except Exception as exc:
                db.log_event("WARNING", "data", f"quote failed for {sig.symbol}: {exc}")
                continue

            # False-breakout veto. It can only block once the model has shown
            # out-of-sample skill; an unproven model blocks nothing.
            hurdle = symbol_cost.estimate_symbol(sig.symbol, obs_cache, coef_cache).hurdle_bps
            # The strategy's own panel, not a 1-minute one. The trainer runs on
            # hourly bars and veto() refuses to answer on a mismatched
            # timeframe, so handing it the minute panel meant the veto had
            # never once been able to express an opinion.
            bo_veto = breakout_mod.veto(spanel, sig.symbol, spanel.T - 1)
            if bo_veto.get("block"):
                db.execute(
                    """INSERT INTO signals(ts, strategy, strategy_version, symbol, side,
                       raw_score, expected_edge_bps, cost_hurdle_bps, decision,
                       reject_reason, features_json, sample_size)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sig.ts, strat.name, strat.version, sig.symbol, sig.side, sig.raw_score,
                     sig.expected_edge_bps, hurdle, "rejected",
                     f"false-breakout veto: {bo_veto['reason']}",
                     json.dumps({**sig.features, "breakout_veto": bo_veto}), 0))
                out["signals"].append({**sig.to_dict(), "decision": "rejected",
                                       "reject_reason": f"false-breakout veto: {bo_veto['reason']}",
                                       "breakout_veto": bo_veto})
                continue

            sym_cost = symbol_cost.estimate_symbol(sig.symbol, obs_cache, coef_cache)
            hurdle = sym_cost.hurdle_bps
            take = sig.expected_edge_bps >= hurdle
            reject_reason = None if take else (
                f"expected edge {sig.expected_edge_bps:.0f} bps < cost hurdle {hurdle:.0f} bps"
            )
            # Paper experiment. The research says these lose; this is how that
            # gets measured rather than debated. Guarded three ways: the setting,
            # paper mode, and live_enabled being false. Real money is never
            # affected by this branch.
            # `mode != "mcp"` was wrong: ADVISORY is also not "mcp", so this branch
            # fired in advisory mode and printed hand-execution tickets for signals
            # the research says lose money. Paper means paper.
            experiment = False
            if (not take and mode == "paper" and not s.live_enabled
                    and getattr(s, "paper_ignore_hurdle", False)):
                take, experiment = True, True
                reject_reason = (f"EXPERIMENT: taken in paper despite edge "
                                 f"{sig.expected_edge_bps:.0f} < hurdle {hurdle:.0f} bps, "
                                 f"to measure what actually happens")
            # This read `and not guards.open_positions(mode)`, so holding ANY
            # position in ANY symbol let a sell signal through and it was opened as
            # a SHORT -- on a venue where shorting crypto is impossible.
            if sig.side == "sell":
                take, reject_reason = False, (
                    "Robinhood does not permit shorting crypto; short signals are not tradeable")

            sig_id = db.execute(
                """INSERT INTO signals(ts, strategy, strategy_version, symbol, side, raw_score,
                                       expected_edge_bps, edge_ci_low_bps, edge_ci_high_bps,
                                       cost_hurdle_bps, decision, reject_reason, features_json, sample_size,
                                       checks_json, field_size, field_rank)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig.ts, strat.name, strat.version, sig.symbol, sig.side, sig.raw_score,
                 sig.expected_edge_bps, sig.edge_ci_bps[0], sig.edge_ci_bps[1], hurdle,
                 ("taken_experiment" if experiment else "taken") if take else "rejected",
                 reject_reason,
                 json.dumps({**sig.features, "stop_bps": sig.stop_bps,
                             "target_bps": sig.target_bps, "hold_seconds": sig.hold_seconds,
                             # Recorded, not enforced. The "one move a coin a day"
                             # rule used to refuse these outright; now the count
                             # rides along so the retrainer can find out whether a
                             # re-entry is actually worse, instead of the rule being
                             # assumed true forever.
                             "prior_entries_today": _prior_entries_today(mode, sig.symbol)}),
                 int(sig.features.get("calibration_n", 0)),
                 # The gate chain is written back by an UPDATE below: the risk
                 # check has not run yet at this point in the tick, and reading
                 # `decision` here is a NameError that the try/except around the
                 # tick would have swallowed on every signal. Caught by py-undef.
                 None,
                 int(len(sigs)),
                 1 + sum(1 for _s in sigs
                         if float(_s.raw_score or 0) > float(sig.raw_score or 0))),
            )
            out["signals"].append({
                **sig.to_dict(),
                "decision": ("taken_experiment" if experiment else "taken") if take else "rejected",
                "reject_reason": reject_reason, "cost_hurdle_bps": hurdle})
            if not take:
                continue

            # A trade budget, if one is set, decides whether this signal is
            # worth spending a slot on or whether to hold out for something
            # better later in the window.
            quality = sig.expected_edge_bps - hurdle
            bud = budget.spend_if_allowed(quality, sig.symbol, mode, strat.name,
                                          experiment=experiment)
            if not bud.take:
                db.execute("UPDATE signals SET decision='rejected', reject_reason=? WHERE id=?",
                           (bud.reason, sig_id))
                out["orders"].append({"symbol": sig.symbol, "blocked": True,
                                      "reasons": [bud.reason],
                                      "budget": bud.to_dict()})
                continue

            # Conviction = how far past its own entry bar this signal is. The
            # strategy knows its threshold; the engine does not invent one.
            # Size on the feature that separates outcomes, not the one that
            # triggers the look. Same 2,605-entry measurement as the sort in
            # volume_build: the volume multiple is flat noise across 2x to 10x+,
            # the 48-hour trend rises monotonically from -2.06% to -1.12% and
            # from 38.5% to 54.1% profitable. Sizing by volume put more money on
            # setups that were no better -- worse than sizing flat.
            # Each strategy states its own conviction as a multiple of its own
            # entry threshold, because what predicts differs between them: the
            # 48-hour trend for volume_build, the depth of the hole for
            # oversold_turn. The engine does not invent a basis of its own.
            _feat = getattr(sig, "features", None) or {}
            _cx = _feat.get("conviction_x")
            try:
                conviction = float(_cx) if _cx is not None and np.isfinite(float(_cx)) else None
            except (TypeError, ValueError):
                conviction = None
            # The stop travels with the size request: a position can only be
            # sized against what it can lose once you know where its exit is.
            notional = feedback.position_size_usd(
                strat.name, mode, conviction=conviction,
                stop_bps=getattr(sig, "stop_bps", None),
                trail_bps=getattr(sig, "trail_bps", None),
                size_mult=float((getattr(sig, "features", None) or {}).get("regime_mult", 1.0) or 1.0))
            decision = guards.pre_trade_check(
                symbol=sig.symbol, side=sig.side, notional_usd=notional, mode=mode,
                feed_agreement=agree, rh_confirmed=bool(row["rh_confirmed"]),
                raw_score=float(sig.raw_score),
                stop_frac=feedback._stop_fraction(getattr(sig, "stop_bps", None),
                                                  getattr(sig, "trail_bps", None)),
                strategy=strat.name,
                # the shape gate needs the target as a fraction of entry; a
                # strategy with no target (pump_ride) passes None and is not
                # judged on a shape it does not have
                field_size=len(sigs),
                target_frac=(float(sig.target_bps) / 1e4
                             if getattr(sig, "target_bps", None)
                             and 0 < float(sig.target_bps) < 90_000 else None),
            )
            # EVERY GATE THAT JUDGED THIS SIGNAL, written back onto its row.
            # 2026-09-23: `reject_reason` only ever held the FIRST failure, as a
            # sentence. The other seventeen checks, their thresholds and their
            # readings were computed on every signal and thrown away.
            try:
                db.execute("UPDATE signals SET checks_json=? WHERE id=?",
                           (json.dumps([{"check": c["check"], "passed": bool(c["passed"]),
                                         "detail": c["detail"], "limit": c["limit"],
                                         "actual": c["actual"]}
                                        for c in decision.checks]), sig_id))
            except Exception:
                pass                # transparency must never block a trade

            try:                       # recorded for the lab: was this a stacked pair?
                _stk = next((c for c in decision.checks if c["check"] == "stacked_position"), None)
                sig.features["stacked_on_n"] = float(_stk["actual"]) if _stk else 0.0
            except Exception:
                pass
            if not decision.allowed:
                # This used to vanish: a risk-blocked order appeared only in the
                # tick's return value, never in the database. So 2,916 taken
                # signals produced zero orders and zero explanation, and the
                # Journal showed nothing at all. Write the reason down.
                db.execute("UPDATE signals SET decision='rejected', reject_reason=? WHERE id=?",
                           ("risk block: " + "; ".join(decision.reasons)[:400], sig_id))
                out["orders"].append({"symbol": sig.symbol, "blocked": True,
                                      "reasons": decision.reasons})
                continue

            fill = get_broker(mode).place(
                OrderRequest(symbol=sig.symbol, side=sig.side, notional_usd=notional,
                             strategy=strat.name, intent="open", signal_id=sig_id,
                             mid_at_decision=q.mid),
                q.mid,
            )
            out["orders"].append({"symbol": sig.symbol, "side": sig.side,
                                  "status": fill.status, "price": fill.price,
                                  "notional": notional, "client_id": fill.client_id})
            if fill.status == "filled":
                # Re-mark BEFORE sizing the next signal. position_size_usd and the
                # cash_available guard both read the newest equity_curve row, and
                # that row is only written after the whole signal loop -- so two
                # signals in one tick each saw the full cash balance and each
                # took 95% of it. A $500 book could open $950 of positions and
                # then write a negative cash figure into the curve that feeds the
                # drawdown kill switch.
                _mark_equity(mode)
                sgn = 1 if sig.side == "buy" else -1
                # INSERT OR REPLACE was wrong, and it cost real book-keeping: the
                # primary key is (symbol, mode, strategy), so a second fill in the
                # same coin REPLACED the first row instead of adding to it. On
                # 2026-09-12 two ENA buys went through 10 minutes apart and
                # 1,630.98 ENA -- $229.37 of a $500 book -- vanished from the
                # ledger while the cash stayed spent. Average in instead.
                _prev = db.query_one(
                    "SELECT qty, avg_px, opened_ts, open_order_id FROM positions "
                    "WHERE symbol=? AND mode=? AND strategy=?",
                    (sig.symbol, mode, strat.name))
                add_qty = sgn * fill.qty
                if _prev and _prev["qty"]:
                    tot = float(_prev["qty"]) + add_qty
                    if abs(tot) < 1e-12:
                        tot = add_qty                       # degenerate; prefer the new fill
                    # Weighted average cost, so stop and target move with the book
                    # rather than snapping to whichever fill happened to be last.
                    avg = ((float(_prev["qty"]) * float(_prev["avg_px"]) + add_qty * fill.price)
                           / tot)
                    opened = float(_prev["opened_ts"])      # the clock runs from the FIRST entry
                else:
                    tot, avg, opened = add_qty, fill.price, time.time()
                # The broker returns a client_id, not the row id, so look the row up.
                # This is the only link between an outcome and the features that
                # produced it; trades.open_order_id was NULL on all 16 rows.
                _oid = db.query_one("SELECT id FROM orders WHERE client_id=?",
                                    (fill.client_id,))
                open_order_id = _oid["id"] if _oid else (
                    _prev["open_order_id"] if _prev and _prev.get("open_order_id") else None)
                db.execute(
                    """INSERT OR REPLACE INTO positions(symbol, mode, strategy, qty, avg_px,
                                                        opened_ts, stop_px, target_px, max_hold_s,
                                                        trail_bps, peak_px, open_order_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sig.symbol, mode, strat.name, tot, avg, opened,
                     avg * (1 - sgn * sig.stop_bps / 1e4),
                     # None, not a number eleven times the price. See
                     # strategy/base.py: a trailing-stop strategy has no target,
                     # and saying that with 100_000 bps put $2.47 on a 22-cent
                     # coin's position row.
                     _base.target_price(avg, sig.target_bps, sgn),
                     sig.hold_seconds,
                     float(getattr(sig, "trail_bps", 0.0) or 0.0), avg, open_order_id),
                )
                # THE PLAN, WRITTEN AT ENTRY. A prediction you can still edit is
                # not a prediction, so this row is inserted once, before any
                # outcome exists, and never rewritten. The engine grades it on
                # every tick from here on; it does not get to revise it.
                try:
                    _tp = trade_plan.build(
                        sig.symbol, strat.name, avg,
                        _base.target_price(avg, sig.target_bps, sgn),
                        avg * (1 - sgn * sig.stop_bps / 1e4),
                        hourly_range_frac(sig.symbol),
                        why={"raw_score": float(sig.raw_score),
                             "expected_edge_bps": float(sig.expected_edge_bps),
                             "cost_hurdle_bps": float(hurdle),
                             "signals_this_bar": int(len(sigs)),
                             # rank by raw_score among this bar's signals for
                             # this strategy -- "out of all signals, why this
                             # one". Computed here rather than read from a
                             # variable that does not exist in this scope.
                             "rank_this_bar": 1 + sum(
                                 1 for _s in sigs
                                 if float(_s.raw_score) > float(sig.raw_score)),
                             "features": dict(sig.features)})
                    trade_plan.record(_tp, mode, opened)
                except Exception:
                    pass        # a plan that cannot be written must not block a fill

    _t = _lap("strategies", _t)
    _mark_equity(mode)
    _lap("mark", _t)
    _done(sum(1 for x in out["signals"] if not x.get("skipped")), len(built))
    return out


def _mark_equity(mode: str, panel: Panel | None = None) -> None:
    """Mark the book to market.

    This used to take a whole panel and read its bottom row -- a matrix of every
    tracked coin, built to price the one or two actually held. The live mid from
    this tick's own quote is both cheaper and fresher; the last stored bar is
    the fallback when a position has not been quoted yet.
    """
    realised = db.query_one("SELECT COALESCE(SUM(net_pnl_usd),0) p FROM trades WHERE mode=?", (mode,))["p"]
    positions = guards.open_positions(mode)
    pv, unreal = 0.0, 0.0
    last = dict(_last_mid)
    if panel is not None and getattr(panel, "T", 0):
        for j, sym in enumerate(panel.symbols):
            last.setdefault(sym, panel.close[-1, j])
    # MARKED AT WHAT YOU WOULD GET, NOT AT THE MID.
    #
    # A position was marked at the mid, so a coin up 1% showed +1% of profit --
    # on a venue whose round trip is 1.92%, closing that position books a LOSS.
    # The operator asked for "profit loss after all side costs estimation, then
    # when final the reporting will be exact", and that is the whole point: the
    # buy-side spread is already inside avg_px, so marking the exit at
    # px * (1 - per_side) makes the open number the same number that gets booked
    # when the position closes. No pleasant surprise on the way in, no unpleasant
    # one on the way out.
    from app.execution import rh_spread
    for p in positions:
        px = last.get(p["symbol"])
        if px is None:
            # This tick has no quote for the coin — most often right after a
            # restart. Ask the stored quotes before falling back to bar closes,
            # which can be an hour old and would mark the book at a stale price.
            from app.data.prices import live_price
            px = live_price(p["symbol"]).px
        if px and np.isfinite(px):
            try:
                side = float(rh_spread.get(p["symbol"])["spread_pct"]) / 100.0
            except Exception:
                side = 0.0095
            exit_px = px * (1.0 - side)
            pv += abs(p["qty"]) * exit_px
            unreal += (exit_px - p["avg_px"]) * p["qty"]
    equity = mode_mod.get_equity() + realised + unreal
    db.execute(
        """INSERT OR REPLACE INTO equity_curve(ts, mode, equity, cash, positions_value,
                                               realised_pnl, unrealised_pnl)
           VALUES (?,?,?,?,?,?,?)""",
        (time.time(), mode, equity, equity - pv, pv, realised, unreal),
    )


# ── loop control ──────────────────────────────────────────────────────────────
def _run() -> None:
    s = get_settings()
    while _state["running"]:
        try:
            tick()
            _state["last_tick"] = time.time()
            _state["ticks"] += 1
            _state["last_error"] = None
        except Exception as exc:
            _state["last_error"] = f"{type(exc).__name__}: {exc}"
            db.log_event("ERROR", "engine", _state["last_error"])

        # DID THE MACHINE SLEEP?
        #
        # The desk needs no browser — it is a thread on its own clock — but it
        # does need the Mac awake. A closed lid stops everything, and on waking
        # the loop simply carries on as though nothing happened, so hours of
        # missing bars and signals look exactly like hours of quiet market.
        # A gap far larger than the poll interval is worth recording as what it
        # is: time the desk did not exist.
        try:
            _prev = _state.get("last_tick_wall")
            _now = time.time()
            _state["last_tick_wall"] = _now
            if _prev and (_now - _prev) > max(s.poll_interval_s, 5) * 10:
                _gap = _now - _prev
                from app.core import journal as _j
                db.log_event("WARNING", "engine",
                             f"the desk was not running for {_gap / 60:.0f} min "
                             f"(machine asleep, or the process was stopped) — "
                             f"no signals or fills exist for that window")
                _j.append("desk_gap", {"seconds": round(_gap, 1),
                                       "from": _prev, "to": _now})
        except Exception:
            pass

        # Pick up new backend code without anyone having to remember to restart.
        # BETWEEN ticks, never inside one, and only when nothing is in flight —
        # see app/core/autoapply.py for why each condition is there.
        try:
            from app.core import autoapply as _aa
            _d = _aa.check()
            if _d["restart"]:
                _aa.apply_now(_d["why"])
        except Exception:
            pass                      # never let this stop the desk trading

        time.sleep(max(s.poll_interval_s, 5))


def start(strategies: list[str] | None = None) -> dict:
    with _lock:
        if _state["running"]:
            return status()
        _state["strategies"] = list(strategies) if strategies else _default_strategies()
        _state["running"] = True
        _set_wanted(True)
        t = threading.Thread(target=_run, daemon=True, name="tc-engine")
        _state["thread"] = t
        t.start()
        db.log_event("INFO", "engine", f"engine started in {mode_mod.get_mode()} mode")
        return status()


ENGINE_WANTED_KEY = "engine_wanted"


def _set_wanted(on: bool) -> None:
    """Remember whether the operator wants the engine running, across restarts."""
    try:
        db.execute(
            "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (ENGINE_WANTED_KEY, "1" if on else "0", time.time()))
    except Exception:
        pass


def wanted() -> bool:
    """Did the operator leave it running, or did he stop it?

    Pressing stop used to change a variable in memory only. The backend
    autostarts the engine on boot, so any restart -- a supervisor restart, a
    crash, a config reload -- brought it straight back, and it looked like the
    stop button did nothing. Now a stop is remembered.
    """
    try:
        row = db.query_one("SELECT value FROM app_state WHERE key=?", (ENGINE_WANTED_KEY,))
        return True if row is None else str(row["value"]).strip() == "1"
    except Exception:
        return True


def stop() -> dict:
    with _lock:
        _state["running"] = False
        _set_wanted(False)
        db.log_event("INFO", "engine", "engine stopped by the operator; it will stay "
                                       "stopped through restarts until started again")
        return status()


def status() -> dict:
    s = get_settings()
    return {
        "running": _state["running"],
        "mode_state": mode_mod.state(),
        "ticks": _state["ticks"],
        "last_tick": _state["last_tick"],
        "last_error": _state["last_error"],
        "strategies": _state["strategies"],
        "mode": mode_mod.get_mode(),
        "poll_interval_s": s.poll_interval_s,
        "broker": get_broker().probe(),
        "safety": s.safety_report(),
        "tick_profile": tick_profile(),
    }
