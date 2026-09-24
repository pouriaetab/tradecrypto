"""False-breakout detection.

The problem, in the operator's words: it looks like something has satisfied the
conditions for the next leg up, and then it goes the other way. That is a bull
trap, and it is not a rare pathology — it is the *modal* outcome of a level being
breached, because a level is where resting stop orders and momentum triggers sit.
Breaking it produces guaranteed liquidity for anyone who wants to sell into it.

Architecture: this is a META-LABELLING problem (Lopez de Prado 2018, ch. 3).
The primary model answers "did a level just break?" — which is nearly mechanical.
The secondary model answers "is this one real?" and that is where the difficulty
and all of the value live. Separating them means the secondary model trains on a
balanced, well-posed binary question instead of trying to predict returns.

    primary   : price crossed level L by more than a noise band     -> an EVENT
    secondary : P(this event fails) from features known AT the break -> a VETO

The veto is a probability, it is calibrated, and it is measured on data the model
never saw. If it is not better than the base rate out of sample, it says so and
blocks nothing.

Why the features are what they are
----------------------------------
Every feature below is a mechanism, not a pattern someone noticed on a chart:

  penetration / close location  A break that closes back near the bar's low has
                                already been rejected by sellers. The single most
                                informative bar-level feature in the literature.
  volume confirmation           A break on no volume has no new participation
                                behind it; a break on huge volume that goes
                                nowhere is absorption — someone large is selling
                                into it. Both are bearish, in opposite ways,
                                which is why volume enters as a level AND as an
                                interaction with progress.
  approach efficiency           Price grinding into a level in a straight line
                                has already spent its energy; price arriving in
                                a chop has stops to run.
  level history                 A level that has failed before fails again. Touch
                                count and prior failures are counted explicitly.
  room to run                   A break with another level 0.3 ATR overhead has
                                nowhere to go.
  market context                A coin breaking out alone while breadth is
                                negative is idiosyncratic and fragile.
  execution reality             Spread widens exactly when you would want to
                                chase. A break you cannot get filled on cheaply
                                is not an opportunity.

References
----------
Lopez de Prado, M. (2018). Advances in Financial Machine Learning. Wiley.
    ch. 3 meta-labelling and the triple-barrier method; ch. 7 purged CV.
Osler, C. (2003). Currency Orders and Exchange Rate Dynamics: An Explanation for
    the Predictive Success of Technical Analysis. Journal of Finance 58(5).
    -- stop-loss clustering around round numbers, the mechanism behind the trap.
Kaufman, P. (1995). Smarter Trading. -- efficiency ratio.
Amihud, Y. (2002). Illiquidity and stock returns. -- price impact per unit volume.
Platt, J. (1999). Probabilistic Outputs for Support Vector Machines. -- calibration.
Brier, G. (1950). Verification of forecasts expressed in terms of probability.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass, asdict, field

import numpy as np

from app.core import db
from app.strategy.base import Panel


# ══════════════════════════════════════════════════════════════════════════════
# Primitives
# ══════════════════════════════════════════════════════════════════════════════
def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate([[close[0]], close[:-1]])
    return np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])


def _rolling_sum_count(x: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Trailing sum and count of FINITE values over the w bars ending at each bar.

    Every rolling statistic in this file is a prefix sum, because recomputing
    them per event was the original O(T^2) bottleneck. Prefix sums have one
    sharp edge: a single NaN anywhere poisons every value after it, since
    cumsum carries it forward forever.

    That edge cost us the entire false-breakout model. The research panel is a
    union of 50 coins on one timestamp grid, so a coin missing a handful of
    candles gets NaN holes. BTC had 27 NaNs in 35,027 bars — 0.08% — and ATR
    was NaN from the first hole onward, so `find_breakouts` returned ZERO
    events for BTC and the veto silently trained on almost nothing.

    So: sum the finite values, count them, and let the caller divide. A gap
    shortens the window rather than destroying the series.
    """
    valid = np.isfinite(x)
    xf = np.where(valid, x, 0.0)
    cs = np.cumsum(np.insert(xf, 0, 0.0))
    cn = np.cumsum(np.insert(valid.astype(float), 0, 0.0))
    idx = np.arange(x.size)
    lo = np.maximum(idx - w + 1, 0)
    return cs[idx + 1] - cs[lo], cn[idx + 1] - cn[lo]


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 60) -> np.ndarray:
    """Average true range, NaN-safe. A window at least half-populated is used;
    anything thinner is NaN, because an ATR from three bars is not an ATR."""
    tr = true_range(high, low, close)
    out = np.full(tr.size, np.nan)
    if tr.size < window:
        return out
    s, n = _rolling_sum_count(tr, window)
    enough = n >= max(2.0, window / 2.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = s / np.maximum(n, 1.0)
    out[window - 1:] = np.where(enough[window - 1:], vals[window - 1:], np.nan)
    return out


def efficiency(x: np.ndarray) -> float:
    """Kaufman efficiency ratio of a 1-D price path: net travel / total path."""
    if x.size < 3:
        return float("nan")
    path = np.abs(np.diff(x)).sum()
    return float((x[-1] - x[0]) / path) if path > 0 else float("nan")


def rolling_vwap(close: np.ndarray, volume: np.ndarray, window: int) -> np.ndarray:
    pv = close * volume
    out = np.full(close.size, np.nan)
    if close.size < window:
        return out
    # NaN-safe for the same reason as atr(): one missing candle must not blank
    # the rest of the series.
    num, n_pv = _rolling_sum_count(pv, window)
    den, _ = _rolling_sum_count(volume, window)
    enough = n_pv >= max(2.0, window / 2.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where((den > 0) & enough, num / np.maximum(den, 1e-12), np.nan)
    out[window - 1:] = vals[window - 1:]
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 1. Levels
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Level:
    price: float
    kind: str                 # pivot_high | pivot_low | donchian | round | volume_node
    touches: int = 1
    volume_at: float = 0.0
    first_idx: int = 0
    last_touch_idx: int = 0
    prior_failures: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def pivots(high: np.ndarray, low: np.ndarray, left: int = 5, right: int = 5) -> list[Level]:
    """Fractal pivots: a bar whose high exceeds `left` bars before and `right` after.

    `right` bars of lag are unavoidable — a pivot is not known until the bars after
    it exist. Every consumer below respects that lag; a level is only usable from
    index (pivot_idx + right) onward, which is enforced in `levels_as_of`.
    """
    out: list[Level] = []
    n = high.size
    for i in range(left, n - right):
        w_h = high[i - left: i + right + 1]
        if np.isfinite(high[i]) and high[i] == np.nanmax(w_h) and np.sum(w_h == high[i]) == 1:
            out.append(Level(float(high[i]), "pivot_high", first_idx=i, last_touch_idx=i))
        w_l = low[i - left: i + right + 1]
        if np.isfinite(low[i]) and low[i] == np.nanmin(w_l) and np.sum(w_l == low[i]) == 1:
            out.append(Level(float(low[i]), "pivot_low", first_idx=i, last_touch_idx=i))
    return out


def round_levels(price: float, n: int = 3) -> list[Level]:
    """Psychologically salient prices. Osler (2003) shows stop orders cluster here,
    which is precisely the fuel a false break needs."""
    if not np.isfinite(price) or price <= 0:
        return []
    mag = 10 ** math.floor(math.log10(price))
    out = []
    for mult in (0.25, 0.5, 1.0):
        step = mag * mult
        if step <= 0:
            continue
        base = math.floor(price / step) * step
        for k in range(-n, n + 2):
            lv = base + k * step
            if lv > 0 and abs(lv - price) / price < 0.25:
                out.append(Level(float(lv), "round"))
    return out


def volume_nodes(close: np.ndarray, volume: np.ndarray, bins: int = 40,
                 top: int = 4) -> list[Level]:
    """High-volume nodes: prices where a lot of business was done, so a lot of
    participants have a position to defend."""
    ok = np.isfinite(close) & np.isfinite(volume)
    if ok.sum() < 50:
        return []
    c, v = close[ok], volume[ok]
    lo, hi = float(np.min(c)), float(np.max(c))
    if hi <= lo:
        return []
    edges = np.linspace(lo, hi, bins + 1)
    idx = np.clip(np.digitize(c, edges) - 1, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    np.add.at(vol_by_bin, idx, v)
    order = np.argsort(vol_by_bin)[::-1][:top]
    return [Level(float((edges[b] + edges[b + 1]) / 2), "volume_node",
                  volume_at=float(vol_by_bin[b])) for b in order]


def cluster_levels(levels: list[Level], tol: float) -> list[Level]:
    """Merge levels within `tol` (an ATR fraction). Touch counts add up — a price
    that is simultaneously a pivot, a round number and a volume node is a much
    stronger level than any one of those alone."""
    if not levels:
        return []
    ordered = sorted(levels, key=lambda l: l.price)
    merged: list[Level] = [ordered[0]]
    for lv in ordered[1:]:
        last = merged[-1]
        if abs(lv.price - last.price) <= tol:
            total = last.touches + lv.touches
            last.price = (last.price * last.touches + lv.price * lv.touches) / total
            last.touches = total
            last.volume_at += lv.volume_at
            last.first_idx = min(last.first_idx, lv.first_idx)
            last.last_touch_idx = max(last.last_touch_idx, lv.last_touch_idx)
            if lv.kind != last.kind:
                last.kind = f"{last.kind}+{lv.kind}" if "+" not in last.kind else last.kind
        else:
            merged.append(lv)
    return merged


def levels_as_of(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 volume: np.ndarray, t: int, lookback: int = 720,
                 pivot_left: int = 5, pivot_right: int = 5) -> list[Level]:
    """Every level KNOWABLE at bar t. The pivot lag is respected: a pivot at index
    i is only admitted once i + pivot_right <= t, so nothing here uses the future."""
    a = max(0, t - lookback)
    h, l, c, v = high[a:t + 1], low[a:t + 1], close[a:t + 1], volume[a:t + 1]
    if c.size < 40:
        return []
    lv: list[Level] = []
    for p in pivots(h, l, pivot_left, pivot_right):
        if p.first_idx + pivot_right <= c.size - 1:
            p.first_idx += a
            p.last_touch_idx += a
            lv.append(p)
    lv += round_levels(float(c[-1]))
    lv += volume_nodes(c, v)
    a_now = atr(high[:t + 1], low[:t + 1], close[:t + 1], 60)
    tol = (a_now[-1] * 0.35) if np.isfinite(a_now[-1]) else float(np.nanstd(c) * 0.1)
    return cluster_levels(lv, max(tol, 1e-12))


# ══════════════════════════════════════════════════════════════════════════════
# 2. Breakout events
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Breakout:
    idx: int
    symbol: str
    level: float
    level_kind: str
    direction: int                 # +1 up, -1 down
    features: dict = field(default_factory=dict)
    label: int | None = None       # 1 genuine, 0 false
    label_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def rolling_mean_std(x: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Trailing mean and sd, vectorised. Recomputing these per event was the
    original O(T^2) bottleneck; they are prefix sums, so compute them once."""
    n = x.size
    valid = np.isfinite(x)
    xf = np.where(valid, x, 0.0)
    c1 = np.cumsum(np.insert(xf, 0, 0.0))
    c2 = np.cumsum(np.insert(xf ** 2, 0, 0.0))
    cn = np.cumsum(np.insert(valid.astype(float), 0, 0.0))
    mu = np.full(n, np.nan); sd = np.full(n, np.nan)
    if n > w:
        # Divide by the number of FINITE observations, not the window width.
        # Dividing by w treated every gap as a real zero and dragged both the
        # mean and the sd toward nothing.
        cnt = cn[w:] - cn[:-w]
        safe = np.maximum(cnt, 1.0)
        enough = cnt >= max(2.0, w / 2.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            m = (c1[w:] - c1[:-w]) / safe
            v = np.maximum((c2[w:] - c2[:-w]) / safe - m ** 2, 0.0)
        m = np.where(enough, m, np.nan)
        v = np.where(enough, v, np.nan)
        mu[w:] = m[:-1] if m.size > n - w else m[: n - w]
        sd[w:] = np.sqrt(v)[: n - w]
    return mu, sd


def _quiet_nanmedian(a: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(a, axis=1)


def _trailing_mean(x: np.ndarray, w: int) -> np.ndarray:
    """Mean of the last w bars INCLUDING the current one. Never the future."""
    s, n = _rolling_sum_count(x, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, s / np.maximum(n, 1.0), np.nan)


def symbol_context(panel: Panel, j: int) -> dict:
    """Everything expensive, computed once per symbol instead of once per event."""
    close = panel.close[:, j]
    high = panel.high[:, j] if panel.high is not None else close
    low = panel.low[:, j] if panel.low is not None else close
    vol = panel.volume[:, j] if panel.volume is not None else np.ones_like(close)
    vmu, vsd = rolling_mean_std(vol, 60)
    cs = panel.close / np.maximum(np.roll(panel.close, 60, axis=0), 1e-12) - 1.0
    cs[:60] = np.nan
    return {
        "close": close, "high": high, "low": low, "vol": vol,
        "atr60": atr(high, low, close, 60),
        "atr20": atr(high, low, close, 20),
        "vwap240": rolling_vwap(close, vol, 240),
        "vol_mu": vmu, "vol_sd": vsd,
        # TRAILING 3-bar mean including t. np.convolve(mode="same") is CENTRED and
        # would include bar t+1 — a one-bar look-ahead, and exactly the kind of
        # bug that makes a model look prescient in backtest and useless live.
        "vol_mu3": _trailing_mean(vol, 3),
        "cross_section": cs,
        # Early bars have no cross-section at all; nanmedian of an all-NaN row is
        # NaN by definition, so suppress the warning rather than pretend otherwise.
        "median_cs": _quiet_nanmedian(cs),
        "btc_idx": panel.symbols.index("BTC") if "BTC" in panel.symbols else None,
    }


def _feature_vector(j: int, sym: str, t: int, lv: Level, direction: int,
                    panel: Panel, a: np.ndarray, spread_bps: float | None,
                    ctx: dict | None = None) -> dict:
    """Everything knowable at the break bar. No future data touches this function."""
    ctx = ctx if ctx is not None else symbol_context(panel, j)
    close, high, low, vol = ctx["close"], ctx["high"], ctx["low"], ctx["vol"]
    at = a[t] if np.isfinite(a[t]) else np.nanstd(close[max(0, t - 60):t + 1])
    at = max(float(at), 1e-12)
    px = close[t]

    rng = high[t] - low[t]
    clv = ((close[t] - low[t]) / rng) if rng > 0 else 0.5
    upper_wick = ((high[t] - max(close[t], panel.close[t - 1, j])) / rng) if rng > 0 else 0.0
    lower_wick = ((min(close[t], panel.close[t - 1, j]) - low[t]) / rng) if rng > 0 else 0.0

    vmu = ctx["vol_mu"][t] if np.isfinite(ctx["vol_mu"][t]) else 0.0
    vsd = ctx["vol_sd"][t] if np.isfinite(ctx["vol_sd"][t]) else 0.0
    volume_z = ((vol[t] - vmu) / vsd) if vsd > 0 else 0.0
    v3 = float(ctx["vol_mu3"][t])
    volume_trend = (v3 / vmu) if vmu > 0 else 1.0

    approach = close[max(0, t - 20):t + 1]
    appr_eff = efficiency(approach)
    bars_since_touch = t - lv.last_touch_idx

    vw_t = ctx["vwap240"][t]
    dist_vwap = ((px - vw_t) / at) if np.isfinite(vw_t) else 0.0

    day = slice(max(0, t - 1439), t + 1)
    dh, dl = float(np.nanmax(high[day])), float(np.nanmin(low[day]))
    range_pos = ((px - dl) / (dh - dl)) if dh > dl else 0.5

    a_short_t = ctx["atr20"][t]
    atr_ratio = (a_short_t / at) if np.isfinite(a_short_t) else 1.0

    up_run = 0
    for k in range(1, min(12, t)):
        if close[t - k + 1] > close[t - k]:
            up_run += 1
        else:
            break

    # room to the next level in the direction of travel
    others = [o.price for o in _LEVEL_CACHE.get((sym, t), []) if o.price != lv.price]
    ahead = [p for p in others if (p > px if direction > 0 else p < px)]
    room = (min(abs(p - px) for p in ahead) / at) if ahead else 5.0

    own = ctx["cross_section"][t, j]
    breadth_ret = ctx["median_cs"][t]
    breadth_ret = float(breadth_ret) if np.isfinite(breadth_ret) else 0.0
    rel_strength = float((own if np.isfinite(own) else 0.0) - breadth_ret)
    b = ctx["btc_idx"]
    lead = float(ctx["cross_section"][t, b]) if b is not None and np.isfinite(ctx["cross_section"][t, b]) else 0.0

    hour = time.gmtime(float(panel.ts[t]) - 6 * 3600).tm_hour   # operator's local hour

    return {
        "penetration_atr": float(direction * (px - lv.price) / at),
        "close_location": float(clv if direction > 0 else 1 - clv),
        "upper_wick": float(upper_wick if direction > 0 else lower_wick),
        "volume_z": float(volume_z),
        "volume_trend": float(volume_trend),
        "approach_efficiency": float(direction * appr_eff if np.isfinite(appr_eff) else 0.0),
        "bars_since_level_touch": float(min(bars_since_touch, 500)),
        "level_touches": float(lv.touches),
        "level_prior_failures": float(lv.prior_failures),
        "level_is_round": 1.0 if "round" in lv.kind else 0.0,
        "level_is_volume_node": 1.0 if "volume_node" in lv.kind else 0.0,
        "dist_from_vwap_atr": float(direction * dist_vwap),
        "range_position": float(range_pos if direction > 0 else 1 - range_pos),
        "atr_expansion": float(atr_ratio),
        "consecutive_runs": float(up_run),
        "room_to_next_level_atr": float(min(room, 5.0)),
        "relative_strength": float(direction * rel_strength * 100),
        "market_breadth_return": float(direction * breadth_ret * 100),
        "leader_return": float(direction * lead * 100),
        "spread_bps": float(spread_bps if spread_bps is not None else 0.0),
        "hour_of_day": float(hour),
    }


FEATURE_NAMES = [
    "penetration_atr", "close_location", "upper_wick", "volume_z", "volume_trend",
    "approach_efficiency", "bars_since_level_touch", "level_touches",
    "level_prior_failures", "level_is_round", "level_is_volume_node",
    "dist_from_vwap_atr", "range_position", "atr_expansion", "consecutive_runs",
    "room_to_next_level_atr", "relative_strength", "market_breadth_return",
    "leader_return", "spread_bps", "hour_of_day",
]

# BOUNDED, deliberately. The first version of this was a plain dict that veto()
# wrote to on every tick and nothing ever cleared. Running overnight at four
# ticks a minute across fifty coins that is well over a hundred thousand entries,
# each holding a list of Level objects — an unbounded leak that eventually took
# the process down. An LRU with a hard cap cannot do that.
_LAST_DATASET_DIAGNOSTICS: dict = {}
_LEVEL_CACHE_MAX = 512
_LEVEL_CACHE: "OrderedDict[tuple, list]" = OrderedDict()


def _cache_put(key: tuple, value: list) -> None:
    _LEVEL_CACHE[key] = value
    _LEVEL_CACHE.move_to_end(key)
    while len(_LEVEL_CACHE) > _LEVEL_CACHE_MAX:
        _LEVEL_CACHE.popitem(last=False)


def find_breakouts(panel: Panel, symbol: str, *, buffer_atr: float = 0.15,
                   lookback: int = 720, min_gap_bars: int = 30,
                   direction: int = 1, max_events: int = 4000,
                   level_refresh: int = 25) -> list[Breakout]:
    """Every bar where price crossed a knowable level by more than a noise band."""
    if symbol not in panel.symbols:
        return []
    j = panel.symbols.index(symbol)
    ctx = symbol_context(panel, j)
    close, high, low, vol = ctx["close"], ctx["high"], ctx["low"], ctx["vol"]
    a = ctx["atr60"]

    events: list[Breakout] = []
    last_idx = -10 ** 9
    failures: dict[float, int] = {}
    start = max(lookback // 4, 80)
    _last_levels: tuple[int, list[Level]] = (-10 ** 9, [])

    for t in range(start, panel.T - 1):
        if t - last_idx < min_gap_bars:
            continue
        if not (np.isfinite(close[t]) and np.isfinite(a[t]) and a[t] > 0):
            continue
        # Levels move slowly; recomputing them every bar is pure waste. They are
        # refreshed every `level_refresh` bars and reused in between, which
        # cannot introduce look-ahead because a stale level set is a SUBSET of
        # what is knowable now.
        if t - _last_levels[0] >= level_refresh or not _last_levels[1]:
            _last_levels = (t, levels_as_of(high, low, close, vol, t, lookback))
        lvls = _last_levels[1]
        if not lvls:
            continue

        band = buffer_atr * a[t]
        prev = close[t - 1]
        for lv in lvls:
            crossed = (prev <= lv.price < close[t] - band) if direction > 0 else \
                      (prev >= lv.price > close[t] + band)
            if not crossed:
                continue
            key = round(lv.price, 10)
            lv.prior_failures = failures.get(key, 0)
            _cache_put((symbol, t), lvls)
            feats = _feature_vector(j, symbol, t, lv, direction, panel, a,
                                    spread_bps=None, ctx=ctx)
            events.append(Breakout(t, symbol, float(lv.price), lv.kind, direction, feats))
            last_idx = t
            break
        if len(events) >= max_events:
            break

    _label(events, panel, direction, failures, ctx=ctx)
    _LEVEL_CACHE.clear()
    return events


# ══════════════════════════════════════════════════════════════════════════════
# 3. Triple-barrier labelling
# ══════════════════════════════════════════════════════════════════════════════
def _label(events: list[Breakout], panel: Panel, direction: int,
           failures: dict[float, int], horizon: int = 120,
           target_atr: float = 1.0, fail_buffer_atr: float = 0.1,
           min_target_bps: float = 200.0, ctx: dict | None = None) -> None:
    """Triple barrier from the break bar.

    upper barrier   entry + max(target_atr x ATR, min_target_bps) -> GENUINE (1)
    lower barrier   back through the level by a buffer            -> FALSE (0)
    vertical        `horizon` bars                                -> resolved by
                                                                     where it sits

    `min_target_bps` matters: a "genuine" breakout that moves 30 bps is not
    genuine for us, because a round trip costs more than that. The label is
    defined in terms of a move we could actually monetise, not a move that merely
    went the right way.
    """
    cache: dict[str, dict] = {}
    for ev in events:
        j = panel.symbols.index(ev.symbol)
        c = ctx if ctx is not None else cache.setdefault(ev.symbol, symbol_context(panel, j))
        close, high, low, a = c["close"], c["high"], c["low"], c["atr60"]
        t = ev.idx
        entry = close[t]
        at = a[t] if np.isfinite(a[t]) else abs(entry) * 0.01
        target_move = max(target_atr * at, entry * min_target_bps / 1e4)
        up_bar = entry + direction * target_move
        fail_bar = ev.level - direction * fail_buffer_atr * at

        label, reason = None, ""
        for u in range(t + 1, min(t + 1 + horizon, panel.T)):
            hi, lo = high[u], low[u]
            if not np.isfinite(hi):
                continue
            hit_target = (hi >= up_bar) if direction > 0 else (lo <= up_bar)
            hit_fail = (lo <= fail_bar) if direction > 0 else (hi >= fail_bar)
            if hit_fail:                                   # pessimistic tie-break
                label, reason = 0, f"closed back through the level within {u - t} bars"
                break
            if hit_target:
                label, reason = 1, f"reached +{target_move / entry * 1e4:.0f} bps in {u - t} bars"
                break
        if label is None:
            u = min(t + horizon, panel.T - 1)
            moved = direction * (close[u] - entry) / entry * 1e4
            label = 1 if moved > min_target_bps * 0.5 else 0
            reason = f"timed out after {horizon} bars at {moved:+.0f} bps"
        ev.label, ev.label_reason = label, reason
        if label == 0:
            k = round(ev.level, 10)
            failures[k] = failures.get(k, 0) + 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. The model — logistic regression, fitted by IRLS, deliberately inspectable
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class LogisticModel:
    names: list[str]
    beta: np.ndarray
    mu: np.ndarray
    sd: np.ndarray
    se: np.ndarray
    n: int
    base_rate: float

    def predict(self, X: np.ndarray) -> np.ndarray:
        Z = (X - self.mu) / np.where(self.sd > 0, self.sd, 1.0)
        Z = np.column_stack([np.ones(Z.shape[0]), Z])
        return 1.0 / (1.0 + np.exp(-np.clip(Z @ self.beta, -30, 30)))

    def coefficients(self) -> list[dict]:
        out = []
        for i, nm in enumerate(["(intercept)"] + self.names):
            b, s = float(self.beta[i]), float(self.se[i]) if i < self.se.size else float("nan")
            z = b / s if s and np.isfinite(s) and s > 0 else float("nan")
            out.append({
                "feature": nm, "coef": b, "std_error": s, "z": z,
                "odds_ratio": float(np.exp(b)),
                "significant_5pct": bool(np.isfinite(z) and abs(z) > 1.96),
                "direction": ("raises P(false breakout)" if b > 0 else "lowers P(false breakout)"),
            })
        return sorted(out, key=lambda r: -abs(r["z"] if np.isfinite(r["z"]) else 0))


def fit_logistic(X: np.ndarray, y: np.ndarray, names: list[str],
                 l2: float = 1.0, iters: int = 60) -> LogisticModel:
    """Newton-Raphson (IRLS) with an L2 penalty. Written out rather than imported
    so every coefficient and its standard error is inspectable on the page."""
    mu, sd = X.mean(axis=0), X.std(axis=0)
    Z = np.column_stack([np.ones(X.shape[0]), (X - mu) / np.where(sd > 0, sd, 1.0)])
    beta = np.zeros(Z.shape[1])
    P = np.eye(Z.shape[1]) * l2
    P[0, 0] = 0.0                                   # never penalise the intercept
    H = P
    for _ in range(iters):
        eta = np.clip(Z @ beta, -30, 30)
        p = 1.0 / (1.0 + np.exp(-eta))
        W = np.clip(p * (1 - p), 1e-6, None)
        grad = Z.T @ (y - p) - P @ beta
        H = Z.T @ (Z * W[:, None]) + P
        try:
            step = np.linalg.solve(H, grad)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.max(np.abs(step)) < 1e-8:
            break
    try:
        se = np.sqrt(np.clip(np.diag(np.linalg.inv(H)), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(beta.size, np.nan)
    return LogisticModel(names, beta, mu, sd, se, int(X.shape[0]), float(y.mean()))


# ── evaluation ───────────────────────────────────────────────────────────────
def auc(y: np.ndarray, p: np.ndarray) -> float:
    """Rank-based AUC (Mann-Whitney), ties handled."""
    pos, neg = p[y == 1], p[y == 0]
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(order.size, dtype=float)
    ranks[order] = np.arange(1, order.size + 1)
    vals = np.concatenate([pos, neg])
    for v in np.unique(vals):
        m = vals == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return float((ranks[:pos.size].sum() - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size))


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    q = np.clip(p, 1e-9, 1 - 1e-9)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))


def calibration(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Reliability diagram: does '70% likely to fail' actually fail 70% of the time?
    An uncalibrated probability is unusable as a veto threshold."""
    edges = np.quantile(p, np.linspace(0, 1, bins + 1))
    edges = np.unique(edges)
    out = []
    for i in range(edges.size - 1):
        m = (p >= edges[i]) & (p <= edges[i + 1] if i == edges.size - 2 else p < edges[i + 1])
        if m.sum() < 3:
            continue
        out.append({"bin": i, "n": int(m.sum()),
                    "predicted": float(p[m].mean()), "observed": float(y[m].mean()),
                    "gap": float(p[m].mean() - y[m].mean())})
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 5. Training with purged walk-forward
# ══════════════════════════════════════════════════════════════════════════════
def build_dataset(panel: Panel, symbols: list[str] | None = None,
                  direction: int = 1, **kw) -> tuple[np.ndarray, np.ndarray, list[Breakout]]:
    _LEVEL_CACHE.clear()
    syms = symbols or panel.symbols
    events: list[Breakout] = []
    failures: dict[str, str] = {}
    per_symbol: dict[str, int] = {}
    for s in syms:
        try:
            found = find_breakouts(panel, s, direction=direction, **kw)
            per_symbol[s] = len(found)
            events.extend(found)
        except Exception as exc:
            # Never swallow this silently. A bare `continue` here is how a
            # model that found nothing looked exactly like a model that had
            # nothing to find.
            failures[s] = f"{type(exc).__name__}: {exc}"
            continue
    if failures:
        try:
            db.log_event("ERROR", "breakout",
                         f"{len(failures)} of {len(syms)} symbols raised while "
                         f"finding breakouts; first: "
                         f"{next(iter(failures.items()))}")
        except Exception:
            pass
    _LAST_DATASET_DIAGNOSTICS.clear()
    _LAST_DATASET_DIAGNOSTICS.update({
        "per_symbol_raw": per_symbol,
        "symbols_with_zero": sorted(k for k, v in per_symbol.items() if v == 0),
        "failures": failures,
    })
    events = [e for e in events if e.label is not None
              and all(np.isfinite(e.features.get(k, np.nan)) for k in FEATURE_NAMES)]
    if not events:
        return np.zeros((0, len(FEATURE_NAMES))), np.zeros(0), []
    X = np.array([[e.features[k] for k in FEATURE_NAMES] for e in events], dtype=float)
    y = np.array([e.label for e in events], dtype=float)
    return X, y, events


def train(panel: Panel, symbols: list[str] | None = None, direction: int = 1,
          n_folds: int = 5, embargo_frac: float = 0.02, l2: float = 1.0) -> dict:
    """Fit and evaluate the veto model with purged, embargoed walk-forward.

    Events are ordered in time and split into contiguous folds. Each fold trains
    on everything BEFORE it (minus an embargo) and is scored out of sample. That
    ordering matters: a random split would let the model see a Tuesday to predict
    the preceding Monday, and every metric would be a lie.

    y = 1 means FALSE breakout, so a high predicted probability is a veto.
    """
    X, y_genuine, events = build_dataset(panel, symbols, direction)
    if X.shape[0] < 60:
        return {"available": False, "n_events": int(X.shape[0]),
                "note": ("fewer than 60 labelled breakouts — backfill more history "
                         "before trusting any model here"),
                "n_events_required": 60}

    order = np.argsort([e.idx for e in events])
    X, events = X[order], [events[i] for i in order]
    y = 1.0 - y_genuine[order]                       # 1 = FALSE breakout

    n = X.shape[0]
    emb = max(int(n * embargo_frac), 1)
    bounds = np.linspace(0, n, n_folds + 1, dtype=int)
    oof_p, oof_y, fold_reports = [], [], []

    for f in range(1, n_folds):
        tr_end = max(bounds[f] - emb, 10)
        te_a, te_b = bounds[f], bounds[f + 1]
        if tr_end < 40 or te_b - te_a < 10:
            continue
        m = fit_logistic(X[:tr_end], y[:tr_end], FEATURE_NAMES, l2=l2)
        p = m.predict(X[te_a:te_b])
        yt = y[te_a:te_b]
        oof_p.append(p); oof_y.append(yt)
        fold_reports.append({
            "fold": f, "train_events": int(tr_end), "test_events": int(te_b - te_a),
            "embargo_events": int(emb),
            "auc": auc(yt, p), "brier": brier(yt, p),
            "base_rate_false": float(yt.mean()),
        })

    if not oof_p:
        return {"available": False, "n_events": n, "note": "not enough events per fold"}

    P, Y = np.concatenate(oof_p), np.concatenate(oof_y)
    full = fit_logistic(X, y, FEATURE_NAMES, l2=l2)

    base = float(Y.mean())
    a = auc(Y, P)
    # Bootstrap CI on AUC — a single AUC point estimate on a few hundred events
    # is a lot noisier than it looks.
    rng = np.random.default_rng(11)
    boots = []
    for _ in range(600):
        idx = rng.integers(0, Y.size, Y.size)
        if Y[idx].sum() in (0, Y[idx].size):
            continue
        boots.append(auc(Y[idx], P[idx]))
    ci = (float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))) if boots else (float("nan"),) * 2

    skill = a > 0.5 and np.isfinite(ci[0]) and ci[0] > 0.5
    return {
        "available": True,
        "direction": "upward breaks" if direction > 0 else "downward breaks",
        "n_events": n,
        "base_rate_false_breakout": base,
        "out_of_sample": {
            "auc": a, "auc_ci95": list(ci),
            "brier": brier(Y, P), "brier_of_base_rate": brier(Y, np.full_like(Y, base)),
            "log_loss": log_loss(Y, P),
            "beats_chance_at_95": bool(skill),
        },
        "calibration": calibration(Y, P),
        "folds": fold_reports,
        "coefficients": full.coefficients(),
        "model": full,
        "verdict": (
            f"Out of sample AUC {a:.3f} (95% CI {ci[0]:.3f}–{ci[1]:.3f}). "
            + ("The veto carries real information and may be used to block trades."
               if skill else
               "The confidence interval includes 0.5, so this model is NOT "
               "distinguishable from guessing and must not block anything yet.")
        ),
        "how_to_read": (
            f"{base:.0%} of these breakouts failed by the triple-barrier definition. "
            "That base rate is what any model has to beat. Brier lower than the "
            "base-rate Brier means the probabilities carry information; the "
            "calibration table shows whether they can be trusted as numbers rather "
            "than just as a ranking."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. The live veto
# ══════════════════════════════════════════════════════════════════════════════
_MODEL: dict = {"model": None, "trained_at": None, "report": None, "threshold": 0.65,
                "granularity": None}


def load_or_train(panel: Panel, force: bool = False) -> dict:
    if _MODEL["model"] is not None and not force:
        return _MODEL["report"]
    rep = train(panel)
    if rep.get("available"):
        _MODEL["model"] = rep.pop("model")
        _MODEL["trained_at"] = time.time()
        try:
            _MODEL["granularity"] = int(panel.bar_seconds())
        except Exception:
            _MODEL["granularity"] = None
        _MODEL["report"] = rep
        db.execute(
            "INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
            (time.time(), "breakout_model", "false_breakout",
             str({"n_events": rep["n_events"]}), str(rep["out_of_sample"])[:8000],
             "usable" if rep["out_of_sample"]["beats_chance_at_95"] else "no skill demonstrated"))
    else:
        rep.pop("model", None)
        _MODEL["report"] = rep
    return _MODEL["report"]


def veto(panel: Panel, symbol: str, t: int | None = None,
         threshold: float | None = None) -> dict:
    """Is this coin sitting on a break that the model expects to fail?

    Returns block=False with a stated reason whenever the model has not
    demonstrated out-of-sample skill. A model that cannot beat chance is not
    allowed to veto anything — that would just be adding noise with extra steps.
    """
    t = panel.T - 1 if t is None else t
    rep = _MODEL.get("report")
    thr = threshold if threshold is not None else _MODEL["threshold"]

    # The trainer runs on HOURLY bars; the live engine ticks on 1-MINUTE bars.
    # Every feature changes meaning across that boundary -- atr60 is 60 hours at
    # training and 60 minutes at inference, and the normalisation is fitted to
    # the hourly distribution. A model asked to judge the wrong timeframe must
    # decline rather than answer confidently on a mis-scaled feature vector.
    trained_g = _MODEL.get("granularity")
    try:
        live_g = int(panel.bar_seconds())
    except Exception:
        live_g = None
    if trained_g and live_g and trained_g != live_g:
        return {"block": False, "p_false": None, "state": "timeframe mismatch",
                "reason": (f"the model was trained on {trained_g}s bars and this panel is "
                           f"{live_g}s — every feature would be mis-scaled, so it does not vote")}

    if not rep or not rep.get("available"):
        return {"block": False, "p_false": None, "state": "no model",
                "reason": "the false-breakout model has not been trained yet"}
    if not rep["out_of_sample"]["beats_chance_at_95"]:
        return {"block": False, "p_false": None, "state": "no skill",
                "reason": ("the model does not beat chance out of sample, so it is "
                           "not permitted to block anything")}
    if symbol not in panel.symbols:
        return {"block": False, "p_false": None, "state": "unknown symbol", "reason": ""}

    j = panel.symbols.index(symbol)
    close, high, low = panel.close[:, j], panel.high[:, j], panel.low[:, j]
    vol = panel.volume[:, j] if panel.volume is not None else np.ones_like(close)
    a = atr(high, low, close, 60)
    if not (np.isfinite(a[t]) and a[t] > 0):
        return {"block": False, "p_false": None, "state": "no volatility estimate", "reason": ""}

    lvls = levels_as_of(high, low, close, vol, t)
    _cache_put((symbol, t), lvls)
    band = 0.15 * a[t]
    hit = None
    for lv in lvls:
        if close[t - 1] <= lv.price < close[t] - band:
            hit = lv
            break
    if hit is None:
        return {"block": False, "p_false": None, "state": "not at a breakout",
                "reason": "price is not breaking a level right now"}

    feats = _feature_vector(j, symbol, t, hit, 1, panel, a, None)
    X = np.array([[feats[k] for k in FEATURE_NAMES]], dtype=float)
    p = float(_MODEL["model"].predict(X)[0])

    coefs = {c["feature"]: c["coef"] for c in _MODEL["model"].coefficients()}
    mu, sd = _MODEL["model"].mu, _MODEL["model"].sd
    contrib = []
    for i, k in enumerate(FEATURE_NAMES):
        z = (feats[k] - mu[i]) / (sd[i] if sd[i] > 0 else 1.0)
        contrib.append({"feature": k, "value": feats[k], "z": float(z),
                        "contribution": float(z * coefs.get(k, 0.0))})
    contrib.sort(key=lambda c: -abs(c["contribution"]))

    return {
        "block": bool(p >= thr),
        "p_false": p, "threshold": thr,
        "state": "breaking a level",
        "level": hit.price, "level_kind": hit.kind, "level_touches": hit.touches,
        "top_drivers": contrib[:5],
        "features": feats,
        "reason": (f"P(false breakout) = {p:.0%}, at or above the {thr:.0%} veto line"
                   if p >= thr else
                   f"P(false breakout) = {p:.0%}, below the {thr:.0%} veto line"),
    }


def status() -> dict:
    rep = _MODEL.get("report") or {}
    return {
        "trained": _MODEL["model"] is not None,
        "trained_at": _MODEL["trained_at"],
        "threshold": _MODEL["threshold"],
        "n_events": rep.get("n_events"),
        "auc": (rep.get("out_of_sample") or {}).get("auc"),
        "usable_as_veto": bool((rep.get("out_of_sample") or {}).get("beats_chance_at_95")),
        "verdict": rep.get("verdict"),
    }


def set_threshold(x: float) -> dict:
    _MODEL["threshold"] = float(np.clip(x, 0.5, 0.99))
    return status()
