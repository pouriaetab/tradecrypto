"""Per-symbol Robinhood markup estimator.

Why this exists
---------------
The operator's observation -- a coin around 0.245 filling near 0.2475 -- is a
single anecdote from a single coin. It is genuinely useful, but it is NOT a
constant: Robinhood's embedded spread varies with how liquid and how volatile
the coin is, and BTC almost certainly costs far less than a low-float meme coin.
Applying one number to fifty coins would be a modelling error dressed up as a
measurement.

So instead of a constant, this module BALL-PARKS each coin's markup from things
we can actually observe on a public feed, and then lets real fills pull the
estimate toward measurement, coin by coin.

The model
---------
For coin i, with universe medians as the reference point:

    log(markup_i) = log(base)
                  + b_spread * log(spread_i / spread_med)
                  + b_vol    * log(vol_i    / vol_med)
                  + b_liq    * log(dv_med   / dv_i)

    markup_i  <- max(markup_i, floor_ticks * tick_bps_i)

`base` is the global markup: the operator's prior at first, the measured global
mean once enough real fills exist.

Signs and rough magnitudes of the coefficients are economics, not curve fitting:

  * b_spread > 0  A dealer quoting off a wider public market quotes wider itself.
  * b_vol    > 0  Inventory risk rises with volatility, and the dealer charges for it.
  * b_liq    > 0  Thinner coins are more expensive to hedge, so they cost more to trade.

They are DEFAULTS, deliberately conservative, and they are refit by least squares
on log-markup as soon as there are fills across enough distinct coins
(`MIN_COINS_FOR_FIT`). Until then the card says "assumed", not "fitted".

Shrinkage
---------
Per-coin measurements are noisy at small n, so the final estimate is an
empirical-Bayes blend:

    markup_final_i = w_i * ballpark_i + (1 - w_i) * measured_mean_i,
    w_i = k / (k + n_i),   k = SHRINK_K

which is the same partial-pooling idea used elsewhere in the operator's work:
believe the coin's own data only in proportion to how much of it there is.

Status labels the UI must honour
--------------------------------
  assumed   -- no fills anywhere; coefficients are priors
  ballpark  -- estimated from observables; no fills for THIS coin
  blended   -- some fills for this coin, still pulled toward the ball-park
  measured  -- enough fills for this coin to stand on its own
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict

import numpy as np

from app.config import get_settings
from app.core import db
from app.execution import cost_model, rh_spread

# Coefficient priors (log-log elasticities). Conservative and documented.
B_SPREAD_PRIOR = 0.35
B_VOL_PRIOR = 0.25
B_LIQ_PRIOR = 0.15
COEF_CLIP = (0.0, 1.0)          # elasticities outside this are not believable

SHRINK_K = 10.0                 # a coin's own data outweighs the ball-park at n > 10
MIN_FILLS_PER_COIN_MEASURED = 15
MIN_COINS_FOR_FIT = 6           # distinct coins with fills before we refit coefficients
FLOOR_TICKS = 1.0               # you cannot be filled better than one tick
OBS_LOOKBACK_DAYS = 7.0


@dataclass
class SymbolCost:
    symbol: str
    markup_bps: float            # one side
    round_trip_bps: float
    hurdle_bps: float
    ci_low_bps: float
    ci_high_bps: float
    status: str                  # assumed | ballpark | blended | measured
    n_fills: int
    drivers: dict                # what pushed this coin above or below the base
    observables: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["round_trip_pct"] = self.round_trip_bps / 100
        d["hurdle_pct"] = self.hurdle_bps / 100
        return d


# ── observables ───────────────────────────────────────────────────────────────
# Cost inputs change on the timescale of hours. Both functions below read every
# stored 1-minute bar in their window into Python dicts -- up to 1.5 million
# rows -- under the one database lock. 2026-09-21: /movers (polled every 30 s
# by its tab) called BOTH on every poll, ~3 s of lock each, while the engine's
# tick queued behind them. One answer per five minutes, shared by every caller.
_MEMO_TTL_S = 300.0
_memo: dict[tuple, tuple[float, object]] = {}


def _memoised(key: tuple, build):
    now = time.time()
    hit = _memo.get(key)
    if hit and (now - hit[0]) < _MEMO_TTL_S:
        return hit[1]
    val = build()
    _memo[key] = (time.time(), val)
    return val


def observables(days: float = OBS_LOOKBACK_DAYS) -> dict[str, dict]:
    """Median reference spread, realised volatility, dollar volume and tick size
    per coin, computed from the quotes and bars we have stored. Shared for
    five minutes across callers (see the note above)."""
    return _memoised(("observables", float(days)), lambda: _observables_now(days))


def _observables_now(days: float = OBS_LOOKBACK_DAYS) -> dict[str, dict]:
    since = time.time() - days * 86400
    rows = db.query(
        """SELECT symbol,
                  COUNT(*) n_quotes,
                  AVG(spread_bps) mean_spread_bps,
                  AVG(mid) mean_mid
           FROM quotes WHERE ts >= ? AND spread_bps IS NOT NULL
           GROUP BY symbol""",
        (since,),
    )
    out: dict[str, dict] = {}
    for r in rows:
        out[r["symbol"]] = {
            "ref_spread_bps": r["mean_spread_bps"],
            "price": r["mean_mid"],
            "n_quotes": r["n_quotes"],
        }

    bar_rows = db.query(
        """SELECT symbol, ts, close, volume FROM bars
           WHERE granularity = 60 AND ts >= ? ORDER BY symbol, ts""",
        (int(since),),
    )
    by_sym: dict[str, list] = {}
    for b in bar_rows:
        by_sym.setdefault(b["symbol"], []).append(b)
    for sym, bars in by_sym.items():
        closes = np.array([b["close"] for b in bars], dtype=float)
        vols = np.array([b["volume"] or 0.0 for b in bars], dtype=float)
        d = out.setdefault(sym, {})
        if closes.size > 5:
            lr = np.diff(np.log(np.maximum(closes, 1e-12)))
            d["realised_vol_bps"] = float(np.std(lr) * 1e4)
            d["price"] = d.get("price") or float(closes[-1])
            d["dollar_volume"] = float(np.sum(vols * closes))
            d["n_bars"] = int(closes.size)
    for sym, d in out.items():
        p = d.get("price") or 0.0
        d["tick_size"] = _tick_size(p)
        d["tick_bps"] = (d["tick_size"] / p * 1e4) if p > 0 else None
    return out


def _tick_size(price: float) -> float:
    """Displayed price granularity. A sub-cent coin's tick is a big fraction of
    its price, and that alone puts a floor under the achievable spread."""
    if price <= 0:
        return 0.0
    if price >= 1000:
        return 0.01
    if price >= 1:
        return 0.0001
    if price >= 0.01:
        return 1e-6
    return 1e-8


# ── coefficient fitting ───────────────────────────────────────────────────────
def _measured_by_symbol(days: float) -> dict[str, tuple[float, int]]:
    since = time.time() - days * 86400
    rows = db.query(
        """SELECT symbol, AVG(half_spread_bps) m, COUNT(*) n
           FROM cost_observations
           WHERE source='measured' AND half_spread_bps IS NOT NULL AND ts >= ?
           GROUP BY symbol""",
        (since,),
    )
    return {r["symbol"]: (float(r["m"]), int(r["n"])) for r in rows}


def fit_coefficients(days: float = 60.0) -> dict:
    """Least squares on log-markup once enough distinct coins have real fills.

    Returns the priors, clearly labelled, until then. This function is the whole
    difference between "we assumed the shape of the cost curve" and "we measured it".
    Shared for five minutes across callers (see the note above observables).
    """
    return _memoised(("fit", float(days)), lambda: _fit_coefficients_now(days))


def _fit_coefficients_now(days: float = 60.0) -> dict:
    obs = observables(days)
    meas = _measured_by_symbol(days)
    usable = [(s, m, n) for s, (m, n) in meas.items()
              if n >= 3 and m > 0 and s in obs and obs[s].get("ref_spread_bps")]
    if len(usable) < MIN_COINS_FOR_FIT:
        return {
            "fitted": False,
            "b_spread": B_SPREAD_PRIOR, "b_vol": B_VOL_PRIOR, "b_liq": B_LIQ_PRIOR,
            "n_coins": len(usable), "n_coins_required": MIN_COINS_FOR_FIT,
            "note": ("coefficients are documented priors, not fitted -- need real fills "
                     f"on at least {MIN_COINS_FOR_FIT} distinct coins"),
        }

    spreads = np.array([obs[s]["ref_spread_bps"] for s, _, _ in usable], dtype=float)
    vols = np.array([obs[s].get("realised_vol_bps") or np.nan for s, _, _ in usable])
    dvs = np.array([obs[s].get("dollar_volume") or np.nan for s, _, _ in usable])
    y = np.log(np.array([m for _, m, _ in usable], dtype=float))

    sp_med = float(np.nanmedian(spreads))
    vol_med = float(np.nanmedian(vols))
    dv_med = float(np.nanmedian(dvs))

    X = np.column_stack([
        np.ones_like(y),
        np.log(np.maximum(spreads, 1e-6) / max(sp_med, 1e-6)),
        np.where(np.isfinite(vols), np.log(np.maximum(vols, 1e-6) / max(vol_med, 1e-6)), 0.0),
        np.where(np.isfinite(dvs), np.log(max(dv_med, 1.0) / np.maximum(dvs, 1.0)), 0.0),
    ])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = float(resid @ resid / dof)
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        se = np.full(4, np.nan)

    return {
        "fitted": True,
        "base_bps": float(math.exp(beta[0])),
        "b_spread": float(np.clip(beta[1], *COEF_CLIP)),
        "b_vol": float(np.clip(beta[2], *COEF_CLIP)),
        "b_liq": float(np.clip(beta[3], *COEF_CLIP)),
        "b_spread_se": float(se[1]), "b_vol_se": float(se[2]), "b_liq_se": float(se[3]),
        "n_coins": len(usable),
        "residual_sd_log": float(math.sqrt(sigma2)),
        "r_squared": float(1 - resid @ resid / max(((y - y.mean()) ** 2).sum(), 1e-12)),
        "reference_medians": {"spread_bps": sp_med, "vol_bps": vol_med, "dollar_volume": dv_med},
        "note": "coefficients fitted by least squares on log markup across coins with real fills",
    }


# ── the estimate ──────────────────────────────────────────────────────────────
def estimate_symbol(symbol: str, obs_cache: dict | None = None,
                    coef_cache: dict | None = None) -> SymbolCost:
    s = get_settings()
    symbol = symbol.upper()
    obs = obs_cache if obs_cache is not None else observables()
    coef = coef_cache if coef_cache is not None else fit_coefficients()
    glob = cost_model.estimate()
    base = coef.get("base_bps") if coef.get("fitted") else glob.per_side_bps

    o = obs.get(symbol, {})
    meas = _measured_by_symbol(OBS_LOOKBACK_DAYS * 8).get(symbol)
    n_fills = meas[1] if meas else 0

    refs = coef.get("reference_medians") or {}
    sp_med = refs.get("spread_bps") or _median(obs, "ref_spread_bps")
    vol_med = refs.get("vol_bps") or _median(obs, "realised_vol_bps")
    dv_med = refs.get("dollar_volume") or _median(obs, "dollar_volume")

    drivers: dict = {}
    log_adj = 0.0
    if o.get("ref_spread_bps") and sp_med:
        t = coef["b_spread"] * math.log(max(o["ref_spread_bps"], 1e-6) / max(sp_med, 1e-6))
        drivers["reference_spread"] = {"value_bps": o["ref_spread_bps"], "median_bps": sp_med,
                                       "effect_log": t}
        log_adj += t
    if o.get("realised_vol_bps") and vol_med:
        t = coef["b_vol"] * math.log(max(o["realised_vol_bps"], 1e-6) / max(vol_med, 1e-6))
        drivers["volatility"] = {"value_bps": o["realised_vol_bps"], "median_bps": vol_med,
                                 "effect_log": t}
        log_adj += t
    if o.get("dollar_volume") and dv_med:
        t = coef["b_liq"] * math.log(max(dv_med, 1.0) / max(o["dollar_volume"], 1.0))
        drivers["liquidity"] = {"dollar_volume": o["dollar_volume"], "median": dv_med,
                                "effect_log": t}
        log_adj += t

    ballpark = base * math.exp(log_adj)
    tick_floor = FLOOR_TICKS * (o.get("tick_bps") or 0.0)
    if tick_floor > ballpark:
        drivers["tick_floor"] = {"tick_bps": o.get("tick_bps"),
                                 "note": "price granularity puts a floor under the spread"}
        ballpark = tick_floor

    if n_fills == 0:
        markup = ballpark
        status = "ballpark" if coef.get("fitted") or o else "assumed"
    else:
        w = SHRINK_K / (SHRINK_K + n_fills)
        markup = w * ballpark + (1 - w) * meas[0]
        status = "measured" if n_fills >= MIN_FILLS_PER_COIN_MEASURED else "blended"
        drivers["shrinkage"] = {"weight_on_ballpark": w, "coin_measured_bps": meas[0],
                                "n_fills": n_fills}

    # Uncertainty: residual scatter of the cross-coin fit, or the global prior's
    # width when the coefficients are still assumed.
    rel_sd = coef.get("residual_sd_log", 0.55) if coef.get("fitted") else 0.55
    lo, hi = markup * math.exp(-1.96 * rel_sd), markup * math.exp(1.96 * rel_sd)

    # ── Robinhood's published spread is a FACT, and it dominates ──────────────
    # Everything above infers what the venue *conditions* imply. But Robinhood
    # does not charge us venue conditions: it charges a fixed published spread
    # per coin, stated on the order ticket (0.95% per side for DOGE). No amount
    # of Coinbase tightness makes Robinhood cheaper, so the published number is
    # a floor, and the inferred markup only matters when it is HIGHER — which
    # would mean we expect extra slippage on top of the spread.
    published = rh_spread.get(symbol)
    if published["per_side_bps"] > markup:
        drivers["robinhood_published_spread"] = {
            "spread_pct": published["spread_pct"],
            "per_side_bps": published["per_side_bps"],
            "source": published["source"],
            "note": ("Robinhood's stated spread exceeds what venue conditions imply, "
                     "so it sets the cost. This is the number on the order ticket, "
                     "not an estimate."),
        }
        markup = published["per_side_bps"]
        status = "published" if published["source"] == "observed" else "published_default"
        lo = hi = markup

    # Exact break-even, not 2 x markup: you buy at m(1+s) and sell at m'(1-s),
    # so the mid must rise (1+s)/(1-s) - 1, slightly more than 2s.
    rt = rh_spread.round_trip_bps(markup / 100.0)
    hurdle = max(rt * s.cost_safety_multiplier, rh_spread.hurdle_bps(symbol)) \
        if status not in ("published", "published_default") \
        else rh_spread.hurdle_bps(symbol)
    return SymbolCost(
        symbol=symbol, markup_bps=markup, round_trip_bps=rt,
        hurdle_bps=hurdle,
        ci_low_bps=rh_spread.round_trip_bps(lo / 100.0),
        # min(hi, 49.0) clamped a BPS value at 49 bps (0.49%) when the intent was
        # to stay under round_trip_bps's 50 PERCENT guard rail. Every coin at the
        # normal ~0.95% spread came out with ci_low 191.8 and ci_high 98.5 -- a
        # confidence interval printed backwards.
        ci_high_bps=rh_spread.round_trip_bps(min(hi, 4900.0) / 100.0),
        status=status, n_fills=n_fills, drivers=drivers,
        observables={k: v for k, v in o.items()},
    )


def _median(obs: dict, key: str) -> float | None:
    vals = [v[key] for v in obs.values() if v.get(key)]
    return float(np.median(vals)) if vals else None


def estimate_universe() -> dict:
    """Cost table for every tradeable coin -- the input to 'which coins can I
    actually afford to trade at this account size?'"""
    obs = observables()
    coef = fit_coefficients()
    syms = [r["symbol"] for r in db.query("SELECT symbol FROM universe WHERE active=1 ORDER BY symbol")]
    rows = [estimate_symbol(s, obs, coef).to_dict() for s in syms]
    rows.sort(key=lambda r: r["round_trip_bps"])
    affordable = [r for r in rows if r["status"] != "assumed" and r["round_trip_bps"] < 100]
    return {
        "coefficients": coef,
        "rows": rows,
        "cheapest": rows[:5],
        "most_expensive": rows[-5:] if len(rows) > 5 else [],
        "n_affordable_under_1pct_round_trip": len(affordable),
        "caveat": (
            "These are ESTIMATES of Robinhood's embedded spread inferred from public "
            "market observables, not measurements of Robinhood. Each coin's number "
            "moves toward measurement as real fills for that coin are recorded. "
            "Status: assumed < ballpark < blended < measured."
        ),
    }
