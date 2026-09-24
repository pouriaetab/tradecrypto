"""The Cost Lab -- what a round trip on Robinhood actually costs.

This module is the foundation of the whole system, because on a $500 account
trading a spread-cost venue, execution cost is larger than almost any edge you
can find. If a round trip costs 100 bps, a strategy must produce more than
100 bps of gross alpha per trade before it returns a single cent. Most published
short-horizon crypto signals do not.

Operator observation that started this
--------------------------------------
the operator, from manual Robinhood trading: with a coin displayed at ~0.245, a market
buy tends to fill nearer 0.247-0.248 (~80-120 bps adverse), and an immediate
round trip is worse still. That is encoded below as a PRIOR -- an explicit,
labelled belief with wide uncertainty -- and it is replaced by measurement as
soon as real fills exist. The system never silently treats a belief as a fact.

Measurement definitions
-----------------------
Let m_d be the mid at the moment of the DECISION, m_s the mid at SUBMIT, and p
the fill price.

  effective half-spread (bps) = side * (p - m_s) / m_s * 1e4
  implementation shortfall    = side * (p - m_d) / m_d * 1e4      [Perold 1988]

with side = +1 for a buy and -1 for a sell, so positive always means "worse for
us". Shortfall minus half-spread is the delay/latency component: how much the
market moved between deciding and submitting.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from app.config import get_settings
from app.core import db
from app.execution import rh_spread
from app.research.stats import Estimate, bootstrap_ci

# ── Prior ─────────────────────────────────────────────────────────────────────
# Belief, not measurement. Deliberately wide, and deliberately pessimistic:
# under-estimating cost is how automated strategies bleed out slowly.
# SUPERSEDED, kept only so old measurements remain interpretable. This was
# "80 bps per side, from manual observation". The real number is published on
# every Robinhood order ticket and is 0.95% per side for DOGE — see
# app/execution/rh_spread.py. The guess understated a round trip by ~30 bps,
# which is larger than most of the edges the strategies were finding.
PRIOR_HALF_SPREAD_BPS = 80.0
PRIOR_SD_BPS = 45.0
# How far the mid can drift between deciding and filling. This is the ONLY part of
# a paper fill that should be random when the venue posts its spread.
MID_DRIFT_SD_BPS = 10.0


def _published_prior(symbol: str | None) -> tuple[float, float, str]:
    """(per-side bps, round-trip bps, source) from Robinhood's own ticket."""
    g = rh_spread.get(symbol or "BTC")
    return (g["per_side_bps"], g["round_trip_bps"],
            "robinhood_published" if g["source"] == "observed" else "robinhood_default")
PRIOR_PSEUDO_OBS = 8          # the prior is worth ~8 real observations
MIN_OBS_FOR_MEASUREMENT = 20  # below this we blend heavily toward the prior


@dataclass
class CostEstimate:
    per_side_bps: float
    round_trip_bps: float
    ci_low_bps: float
    ci_high_bps: float
    n_observations: int
    source: str            # "prior" | "blended" | "measured"
    symbol: str | None
    hurdle_bps: float      # what a strategy must beat, incl. safety multiplier

    def to_dict(self) -> dict:
        return {
            "per_side_bps": self.per_side_bps,
            "round_trip_bps": self.round_trip_bps,
            "ci_low_bps": self.ci_low_bps,
            "ci_high_bps": self.ci_high_bps,
            "n_observations": self.n_observations,
            "source": self.source,
            "symbol": self.symbol,
            "hurdle_bps": self.hurdle_bps,
            "hurdle_pct": self.hurdle_bps / 100.0,
            "plain_english": (
                f"A full buy-then-sell round trip is expected to cost about "
                f"{self.round_trip_bps/100:.2f}% of the traded amount. A strategy "
                f"must earn more than {self.hurdle_bps/100:.2f}% gross per trade "
                f"(cost x safety margin) before it makes money."
            ),
        }


def _side_sign(side: str) -> int:
    return 1 if side.lower() in {"buy", "b", "long"} else -1


def record_observation(
    *,
    symbol: str,
    side: str,
    notional_usd: float,
    fill_px: float,
    mid_at_submit: float | None,
    mid_at_decision: float | None = None,
    quoted_spread_bps: float | None = None,
    latency_ms: float | None = None,
    realised_vol_bps: float | None = None,
    mode: str = "paper",
    source: str = "measured",
) -> dict:
    """Write one execution-cost data point. Called on every fill, in every mode."""
    sgn = _side_sign(side)
    half = None
    if mid_at_submit and mid_at_submit > 0:
        half = sgn * (fill_px - mid_at_submit) / mid_at_submit * 1e4
    shortfall = None
    if mid_at_decision and mid_at_decision > 0:
        shortfall = sgn * (fill_px - mid_at_decision) / mid_at_decision * 1e4

    db.execute(
        """INSERT INTO cost_observations
           (ts, symbol, side, notional_usd, mid_at_decision, mid_at_submit, fill_px,
            quoted_spread_bps, half_spread_bps, shortfall_bps, latency_ms,
            realised_vol_bps, mode, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (time.time(), symbol.upper(), side.lower(), notional_usd, mid_at_decision,
         mid_at_submit, fill_px, quoted_spread_bps, half, shortfall, latency_ms,
         realised_vol_bps, mode, source),
    )
    return {"half_spread_bps": half, "shortfall_bps": shortfall,
            "delay_component_bps": (shortfall - half) if (half is not None and shortfall is not None) else None}


def _observations(symbol: str | None, mode: str | None, days: float) -> list[float]:
    since = time.time() - days * 86400
    sql = ("SELECT half_spread_bps FROM cost_observations "
           "WHERE half_spread_bps IS NOT NULL AND ts >= ? AND source='measured'")
    params: list = [since]
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol.upper())
    if mode:
        sql += " AND mode = ?"
        params.append(mode)
    return [r["half_spread_bps"] for r in db.query(sql, params)]


def estimate(symbol: str | None = None, mode: str | None = None, days: float = 30.0) -> CostEstimate:
    """Best current estimate of one-side execution cost, in basis points.

    Blending rule: posterior mean of a Normal-Normal conjugate update, so the
    prior dominates when we have almost no fills and fades out as evidence
    accumulates. There is no point at which the number is invented -- it is
    always either the prior (labelled) or a measurement (with a CI).
    """
    s = get_settings()
    obs = _observations(symbol, mode, days)
    n = len(obs)

    if n == 0:
        # No fills of our own yet — but we are not guessing. Robinhood publishes
        # the spread on the ticket, so the round trip is KNOWN, not estimated.
        # The safety multiplier does not apply to a known quantity; the named
        # slippage and margin terms in rh_spread do that job instead.
        per_side, rt, src = _published_prior(symbol)
        return CostEstimate(
            per_side_bps=per_side,
            round_trip_bps=rt,
            ci_low_bps=rt,           # a published number has no sampling error
            ci_high_bps=rt,
            n_observations=0,
            source=src,
            symbol=symbol,
            hurdle_bps=rh_spread.hurdle_bps(symbol or "BTC"),
        )

    a = np.asarray(obs, dtype=float)
    sample_mean = float(np.mean(a))
    # Normal-Normal blend: weight prior as PRIOR_PSEUDO_OBS observations.
    prior_per_side, _, _ = _published_prior(symbol)
    w = PRIOR_PSEUDO_OBS / (PRIOR_PSEUDO_OBS + n)
    blended = w * prior_per_side + (1 - w) * sample_mean

    if n >= 5:
        _, lo, hi = bootstrap_ci(a, np.mean, block=2.0, n_boot=2000)
    else:
        lo = hi = float("nan")

    src = "measured" if n >= MIN_OBS_FOR_MEASUREMENT else "blended"
    per_side = sample_mean if src == "measured" else blended
    # (1+s)/(1-s)-1, matching rh_spread.round_trip_bps. The 2s approximation was
    # removed from the backtester and from rh_spread; leaving it here meant the
    # live cost model disagreed with both.
    def _rt(side_bps: float) -> float:
        x = side_bps / 1e4
        return ((1 + x) / (1 - x) - 1) * 1e4 if 0 <= x < 0.5 else 2 * side_bps

    rt = _rt(per_side)
    return CostEstimate(
        per_side_bps=per_side,
        round_trip_bps=rt,
        ci_low_bps=_rt(lo) if math.isfinite(lo) else float("nan"),
        ci_high_bps=_rt(hi) if math.isfinite(hi) else float("nan"),
        n_observations=n,
        source=src,
        symbol=symbol,
        hurdle_bps=rt * s.cost_safety_multiplier,
    )


def hurdle_bps(symbol: str | None = None) -> float:
    """The number every signal is measured against. Nothing trades below it.

    Floored at the published Robinhood hurdle: our own fills can look cheap by
    luck on a small sample, and no amount of good luck makes the spread smaller
    than the spread.
    """
    est = estimate(symbol).hurdle_bps
    floor = rh_spread.hurdle_bps(symbol or "BTC")
    return max(est, floor)


def breakdown(symbol: str | None = None, days: float = 30.0) -> dict:
    """Full Cost Lab report for the dashboard."""
    since = time.time() - days * 86400
    sql = "SELECT * FROM cost_observations WHERE ts >= ?"
    params: list = [since]
    if symbol:
        sql += " AND symbol=?"
        params.append(symbol.upper())
    rows = db.query(sql + " ORDER BY ts DESC LIMIT 5000", params)

    half = np.array([r["half_spread_bps"] for r in rows
                     if r["half_spread_bps"] is not None and r["source"] == "measured"], dtype=float)
    short = np.array([r["shortfall_bps"] for r in rows
                      if r["shortfall_bps"] is not None and r["source"] == "measured"], dtype=float)
    quoted = np.array([r["quoted_spread_bps"] for r in rows
                       if r["quoted_spread_bps"] is not None], dtype=float)

    def _est(a: np.ndarray, label: str, units: str = "bps") -> dict:
        if a.size == 0:
            return Estimate(None, 0, method=label, units=units, min_n=MIN_OBS_FOR_MEASUREMENT).to_dict()
        p, lo, hi = bootstrap_ci(a, np.median if a.size >= 10 else np.mean, block=2.0, n_boot=2000)
        return Estimate(p, int(a.size), lo, hi, method=label, units=units,
                        min_n=MIN_OBS_FOR_MEASUREMENT).to_dict()

    est = estimate(symbol, days=days)

    # Per-symbol table -- some coins are far more expensive than others, and
    # that alone can decide which are tradeable at this account size.
    per_symbol = db.query(
        """SELECT symbol,
                  COUNT(*) n,
                  AVG(half_spread_bps) mean_half_bps,
                  AVG(shortfall_bps)   mean_shortfall_bps,
                  AVG(quoted_spread_bps) mean_quoted_bps
           FROM cost_observations
           WHERE ts >= ? AND source='measured' AND half_spread_bps IS NOT NULL
           GROUP BY symbol ORDER BY n DESC""",
        [since],
    )

    return {
        "current_estimate": est.to_dict(),
        "effective_half_spread": _est(half, "median of measured (fill vs mid at submit), BCa bootstrap CI"),
        "implementation_shortfall": _est(short, "Perold (1988) shortfall vs mid at decision"),
        "quoted_spread": _est(quoted, "quoted (ask-bid)/mid from the reference feed"),
        "delay_component_bps": (
            float(np.median(short) - np.median(half)) if half.size and short.size else None
        ),
        "per_symbol": per_symbol,
        "n_total_observations": len(rows),
        "prior": {
            "half_spread_bps": PRIOR_HALF_SPREAD_BPS,
            "sd_bps": PRIOR_SD_BPS,
            "pseudo_observations": PRIOR_PSEUDO_OBS,
            "origin": "operator's manual Robinhood fills (~0.245 quoted -> ~0.247/0.248 filled)",
            "status": "belief, superseded by measurement once n >= %d" % MIN_OBS_FOR_MEASUREMENT,
        },
        "why_this_matters": (
            "Break-even gross move per round trip is roughly "
            f"{est.round_trip_bps/100:.2f}%. Any strategy whose average gross move "
            "is smaller than that loses money with perfect forecasting."
        ),
    }


def simulate_fill(mid: float, side: str, symbol: str | None = None,
                  rng: np.random.Generator | None = None) -> tuple[float, float]:
    """Paper-mode fill price: mid moved against us by a draw from the cost model.

    Deliberately stochastic. A paper engine that always fills at the mid is a
    machine for generating false confidence.
    """
    rng = rng or np.random.default_rng()
    est = estimate(symbol)
    # `est.source == "prior"` was NEVER TRUE. estimate() returns
    # "robinhood_published" / "robinhood_default" when it has no fills, and
    # "measured" / "blended" once it does -- "prior" is only ever written into
    # cost_observations.source by the paper broker, which is a different field.
    # So the whole deterministic branch below was unreachable and every paper
    # fill kept drawing from Normal(95bps, 38bps): about one fill in six cheaper
    # than 50 bps, on the one number this entire project turns on.
    if est.source in ("robinhood_published", "robinhood_default"):
        # Robinhood's spread is a POSTED PRICE, not a random variable. estimate()
        # already says so -- it returns ci_low == ci_high for exactly this reason
        # -- and then this function used to draw it from Normal(95bps, 45bps).
        # That produced fills as cheap as 38 bps, which is half the real cost, on
        # roughly half of all paper trades. On a project whose entire question is
        # whether the edge beats the spread, understating the spread half the time
        # is the worst possible error.
        #
        # The spread is now charged in full, every time, on BOTH sides, for EVERY
        # coin (measured per coin where we have it, 0.95% where we do not). The
        # only random part is what is genuinely random: the mid drifting between
        # the decision and the fill.
        draw_bps = max(0.0, est.per_side_bps + float(rng.normal(0.0, MID_DRIFT_SD_BPS)))
    else:
        sd = max(PRIOR_SD_BPS * 0.5, abs(est.per_side_bps) * 0.4)
        draw_bps = max(0.0, float(rng.normal(est.per_side_bps, sd)))
    sgn = _side_sign(side)
    return mid * (1 + sgn * draw_bps / 1e4), draw_bps
