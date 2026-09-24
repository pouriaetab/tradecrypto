"""Statistical machinery. Every function here is cited and unit-tested.

The point of this module is to stop us fooling ourselves. A backtest that looks
good is the default outcome of searching over strategies; these tools estimate
how much of that "good" is luck and selection.

References
----------
[1] Bailey, D. & Lopez de Prado, M. (2012). "The Sharpe Ratio Efficient Frontier."
    Journal of Risk 15(2). -- Probabilistic Sharpe Ratio.
[2] Bailey, D. & Lopez de Prado, M. (2014). "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and Non-Normality."
    Journal of Portfolio Management 40(5).
[3] Bailey, Borwein, Lopez de Prado & Zhu (2017). "The Probability of Backtest
    Overfitting." Journal of Computational Finance 20(4). -- CSCV / PBO.
[4] Politis, D. & Romano, J. (1994). "The Stationary Bootstrap." JASA 89(428).
[5] Efron, B. (1987). "Better Bootstrap Confidence Intervals." JASA 82(397). -- BCa.
[6] Kelly, J. L. (1956). "A New Interpretation of Information Rate." Bell System TJ.
[7] Perold, A. (1988). "The Implementation Shortfall: Paper versus Reality."
    Journal of Portfolio Management 14(3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from itertools import combinations
from typing import Callable, Sequence

import numpy as np
from scipy import stats as sps

EULER_GAMMA = 0.5772156649015329


# ──────────────────────────────────────────────────────────────────────────────
# Result container
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Estimate:
    """A number you are allowed to show a human.

    It refuses to exist without a sample size, and it carries its own
    uncertainty. `sufficient` is False when n is too small to say anything;
    the UI renders those as "insufficient evidence" rather than a number.
    """

    value: float | None
    n: int
    ci_low: float | None = None
    ci_high: float | None = None
    method: str = ""
    units: str = ""
    min_n: int = 30
    notes: str = ""

    @property
    def sufficient(self) -> bool:
        return self.value is not None and np.isfinite(self.value) and self.n >= self.min_n

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sufficient"] = self.sufficient
        for k in ("value", "ci_low", "ci_high"):
            v = d[k]
            if v is not None and not np.isfinite(v):
                d[k] = None
        return d


def _clean(x: Sequence[float]) -> np.ndarray:
    a = np.asarray(list(x), dtype=float)
    return a[np.isfinite(a)]


# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────────────────────
def stationary_bootstrap_indices(n: int, expected_block: float, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap [4].

    Geometric block lengths with mean `expected_block` preserve short-range
    dependence, which matters because trading returns are autocorrelated and a
    plain iid bootstrap understates the variance of the mean.
    """
    if expected_block <= 1:
        return rng.integers(0, n, size=n)
    p = 1.0 / expected_block
    idx = np.empty(n, dtype=int)
    i = int(rng.integers(0, n))
    for t in range(n):
        idx[t] = i
        if rng.random() < p:
            i = int(rng.integers(0, n))
        else:
            i = (i + 1) % n
    return idx


def bootstrap_ci(
    x: Sequence[float],
    stat: Callable[[np.ndarray], float] = np.mean,
    alpha: float = 0.05,
    n_boot: int = 5000,
    block: float | None = None,
    seed: int = 7,
) -> tuple[float, float, float]:
    """Return (point, ci_low, ci_high).

    Uses the stationary bootstrap when `block` is given (time series), otherwise
    an iid bootstrap, and BCa correction [5] for skew/bias in the statistic.
    """
    a = _clean(x)
    n = a.size
    if n < 5:
        return (float(stat(a)) if n else float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    point = float(stat(a))

    boots = np.empty(n_boot)
    for b in range(n_boot):
        idx = (
            stationary_bootstrap_indices(n, block, rng)
            if block
            else rng.integers(0, n, size=n)
        )
        boots[b] = stat(a[idx])

    # BCa
    z0 = sps.norm.ppf(np.clip((boots < point).mean(), 1e-6, 1 - 1e-6))
    jack = np.array([stat(np.delete(a, i)) for i in range(n)]) if n <= 400 else None
    if jack is not None:
        jbar = jack.mean()
        num = ((jbar - jack) ** 3).sum()
        den = 6.0 * (((jbar - jack) ** 2).sum() ** 1.5)
        acc = num / den if den != 0 else 0.0
    else:
        acc = 0.0

    zl, zu = sps.norm.ppf(alpha / 2), sps.norm.ppf(1 - alpha / 2)
    def adj(z):
        d = 1 - acc * (z0 + z)
        return sps.norm.cdf(z0 + (z0 + z) / d) if d != 0 else sps.norm.cdf(z0 + z)

    lo_q, hi_q = adj(zl), adj(zu)
    lo_q, hi_q = float(np.clip(lo_q, 0.001, 0.999)), float(np.clip(hi_q, 0.001, 0.999))
    return point, float(np.quantile(boots, lo_q)), float(np.quantile(boots, hi_q))


# ──────────────────────────────────────────────────────────────────────────────
# Sharpe family
# ──────────────────────────────────────────────────────────────────────────────
def sharpe(returns: Sequence[float], periods_per_year: float | None = None) -> float:
    r = _clean(returns)
    if r.size < 2 or r.std(ddof=1) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    return float(sr * math.sqrt(periods_per_year)) if periods_per_year else float(sr)


def probabilistic_sharpe(
    returns: Sequence[float], sr_benchmark: float = 0.0
) -> float:
    """PSR [1]: P(true per-period Sharpe > benchmark), adjusted for skew/kurtosis.

    Both `sr_benchmark` and the estimated Sharpe are PER-PERIOD (not annualised).
    """
    r = _clean(returns)
    n = r.size
    if n < 8:
        return float("nan")
    sr = r.mean() / r.std(ddof=1) if r.std(ddof=1) > 0 else float("nan")
    if not np.isfinite(sr):
        return float("nan")
    g3 = float(sps.skew(r, bias=False))
    g4 = float(sps.kurtosis(r, fisher=False, bias=False))  # non-excess
    denom = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr**2
    if denom <= 0:
        return float("nan")
    z = (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(sps.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] under the null that every trial has true Sharpe 0 [2].

    This is the bar a backtested Sharpe must clear just to be interesting.
    """
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    a = sps.norm.ppf(1.0 - 1.0 / n_trials)
    b = sps.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(
    returns: Sequence[float], n_trials: int, trial_sharpes: Sequence[float] | None = None
) -> dict:
    """DSR [2]: PSR measured against the Sharpe you'd expect from luck alone
    after `n_trials` configurations were searched.

    DSR < 0.95 means: after accounting for how many things you tried, you cannot
    reject "this strategy has no edge" at the 5% level.
    """
    r = _clean(returns)
    if trial_sharpes is not None and len(trial_sharpes) > 1:
        sr_var = float(np.var(_clean(trial_sharpes), ddof=1))
    else:
        # Fallback: variance of a Sharpe estimate under the null, ~1/(n-1).
        sr_var = 1.0 / max(r.size - 1, 1)
    sr0 = expected_max_sharpe(max(n_trials, 1), sr_var)
    return {
        "deflated_sharpe": probabilistic_sharpe(r, sr_benchmark=sr0),
        "sr_benchmark_from_selection": sr0,
        "sr_observed_per_period": sharpe(r),
        "n_trials": int(n_trials),
        "sr_variance_across_trials": sr_var,
        "n_observations": int(r.size),
    }


def min_track_record_length(
    returns: Sequence[float], sr_benchmark: float = 0.0, confidence: float = 0.95
) -> float:
    """How many observations you need before the Sharpe is distinguishable
    from `sr_benchmark` at `confidence` [1]. Returns +inf if never.
    """
    r = _clean(returns)
    if r.size < 8 or r.std(ddof=1) == 0:
        return float("inf")
    sr = r.mean() / r.std(ddof=1)
    if sr <= sr_benchmark:
        return float("inf")
    g3 = float(sps.skew(r, bias=False))
    g4 = float(sps.kurtosis(r, fisher=False, bias=False))
    z = sps.norm.ppf(confidence)
    return float(1 + (1 - g3 * sr + ((g4 - 1) / 4) * sr**2) * (z / (sr - sr_benchmark)) ** 2)


# ──────────────────────────────────────────────────────────────────────────────
# Probability of Backtest Overfitting (CSCV) [3]
# ──────────────────────────────────────────────────────────────────────────────
def pbo_cscv(perf_matrix: np.ndarray, s: int = 8) -> dict:
    """Combinatorially Symmetric Cross-Validation.

    perf_matrix : (T observations, N configurations) of per-period returns.
    Splits the timeline into `s` contiguous chunks, takes every half as train,
    picks the config with best in-sample Sharpe, and records its RANK
    out-of-sample. If selection were skill, the winner would keep winning.

    Returns PBO = P(out-of-sample rank of the in-sample winner is below median).
    """
    X = np.asarray(perf_matrix, dtype=float)
    if X.ndim != 2 or X.shape[1] < 2:
        return {"pbo": float("nan"), "n_splits": 0, "note": "need >=2 configurations"}
    T, N = X.shape
    s = max(2, s - (s % 2))
    if T < s * 4:
        return {"pbo": float("nan"), "n_splits": 0, "note": f"need >= {s*4} observations, have {T}"}

    chunks = np.array_split(np.arange(T), s)
    logits: list[float] = []
    for train_ids in combinations(range(s), s // 2):
        test_ids = [i for i in range(s) if i not in train_ids]
        tr = np.concatenate([chunks[i] for i in train_ids])
        te = np.concatenate([chunks[i] for i in test_ids])

        def _sr(block: np.ndarray) -> np.ndarray:
            mu = block.mean(axis=0)
            sd = block.std(axis=0, ddof=1)
            out = np.divide(mu, sd, out=np.zeros_like(mu), where=sd > 0)
            return out

        sr_tr, sr_te = _sr(X[tr]), _sr(X[te])
        best = int(np.argmax(sr_tr))
        # relative rank of the winner out of sample, in (0,1)
        rank = (sps.rankdata(sr_te)[best]) / (N + 1)
        rank = float(np.clip(rank, 1e-6, 1 - 1e-6))
        logits.append(math.log(rank / (1 - rank)))

    lg = np.array(logits)
    return {
        "pbo": float((lg <= 0).mean()),
        "n_splits": int(lg.size),
        "median_logit": float(np.median(lg)),
        "interpretation": (
            "PBO is the probability that the configuration which looked best "
            "in-sample performs below median out-of-sample. Above ~0.5 the "
            "selection process is worse than random."
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Performance summary
# ──────────────────────────────────────────────────────────────────────────────
def drawdown(equity: Sequence[float]) -> dict:
    e = _clean(equity)
    if e.size < 2:
        return {"max_drawdown_pct": float("nan"), "peak": float("nan")}
    peak = np.maximum.accumulate(e)
    dd = (e - peak) / np.where(peak == 0, np.nan, peak)
    i = int(np.nanargmin(dd))
    return {
        "max_drawdown_pct": float(-dd[i] * 100),
        "trough_index": i,
        "peak_index": int(np.nanargmax(e[: i + 1])) if i > 0 else 0,
    }


def performance_summary(
    net_returns: Sequence[float],
    periods_per_year: float,
    n_trials: int = 1,
    trial_sharpes: Sequence[float] | None = None,
) -> dict:
    """The full honest scorecard for a return stream."""
    r = _clean(net_returns)
    n = r.size
    if n == 0:
        return {"n": 0, "sufficient": False, "note": "no trades"}

    mean_point, mean_lo, mean_hi = bootstrap_ci(r, np.mean, block=max(2.0, n**0.25))
    equity = np.cumprod(1 + r)
    wins = r > 0
    gross_win = r[wins].sum()
    gross_loss = -r[~wins].sum()

    dsr = deflated_sharpe(r, n_trials=n_trials, trial_sharpes=trial_sharpes)
    downside = r[r < 0]
    sortino = (
        float(r.mean() / downside.std(ddof=1) * math.sqrt(periods_per_year))
        if downside.size > 1 and downside.std(ddof=1) > 0
        else float("nan")
    )

    return {
        "n": int(n),
        "sufficient": n >= 30,
        "mean_return_per_trade": mean_point,
        "mean_return_ci": [mean_lo, mean_hi],
        "mean_return_bps": mean_point * 1e4,
        "positive_mean_at_95": bool(np.isfinite(mean_lo) and mean_lo > 0),
        "total_return_pct": float((equity[-1] - 1) * 100),
        "sharpe_annualised": sharpe(r, periods_per_year),
        "sharpe_per_trade": sharpe(r),
        "sortino_annualised": sortino,
        "probabilistic_sharpe": probabilistic_sharpe(r),
        "deflated_sharpe": dsr["deflated_sharpe"],
        "deflated_sharpe_detail": dsr,
        "min_track_record_length": min_track_record_length(r),
        "hit_rate": float(wins.mean()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win_bps": float(r[wins].mean() * 1e4) if wins.any() else 0.0,
        "avg_loss_bps": float(r[~wins].mean() * 1e4) if (~wins).any() else 0.0,
        "max_drawdown": drawdown(equity),
        "skew": float(sps.skew(r, bias=False)) if n > 3 else None,
        "excess_kurtosis": float(sps.kurtosis(r, bias=False)) if n > 3 else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Sizing
# ──────────────────────────────────────────────────────────────────────────────
def kelly_fraction_continuous(mean: float, var: float) -> float:
    """f* = mu / sigma^2 [6]. The growth-optimal fraction for small edges.

    Only correct if mu and sigma are known exactly. They never are, which is why
    the engine multiplies this by TC_KELLY_FRACTION (<= 0.5) and by an
    uncertainty haircut derived from the posterior's confidence interval.
    """
    if var <= 0 or not np.isfinite(mean):
        return 0.0
    return float(max(0.0, mean / var))


def kelly_with_uncertainty(mean_ci_low: float, var: float, cap: float = 0.25) -> float:
    """Size on the LOWER confidence bound of the edge, not the point estimate.

    This is the single most effective defence against overbetting a strategy
    whose edge was estimated from a small sample.
    """
    if not np.isfinite(mean_ci_low) or mean_ci_low <= 0 or var <= 0:
        return 0.0
    return float(min(cap, mean_ci_low / var))


# ──────────────────────────────────────────────────────────────────────────────
# Bayesian edge posteriors (the feedback loop's memory)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class BetaBinomial:
    """Posterior over a strategy's hit rate. Conjugate, so updates are exact."""

    alpha: float = 1.0
    beta: float = 1.0

    def update(self, wins: float, losses: float) -> "BetaBinomial":
        """wins/losses may be fractional: weighted counts from versions.py."""
        return BetaBinomial(self.alpha + float(wins), self.beta + float(losses))

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def ci(self, level: float = 0.9) -> tuple[float, float]:
        a = (1 - level) / 2
        return (
            float(sps.beta.ppf(a, self.alpha, self.beta)),
            float(sps.beta.ppf(1 - a, self.alpha, self.beta)),
        )

    def sample(self, rng: np.random.Generator) -> float:
        return float(rng.beta(self.alpha, self.beta))

    def to_dict(self) -> dict:
        lo, hi = self.ci()
        return {"alpha": self.alpha, "beta": self.beta, "mean": self.mean,
                "ci90": [lo, hi], "n_effective": self.alpha + self.beta - 2}


@dataclass
class NormalInverseGamma:
    """Posterior over per-trade net return (mean and variance both unknown).

    Conjugate NIG(mu0, kappa, a, b). Used to answer the only question that
    matters for allocation: is this strategy's net edge above the cost hurdle,
    and how sure are we?
    """

    mu0: float = 0.0
    kappa: float = 1.0
    a: float = 2.0
    b: float = 1e-6

    def update(self, x: Sequence[float], weights: Sequence[float] | None = None) -> "NormalInverseGamma":
        """Conjugate update. With `weights`, each observation counts w times:
        n becomes the sum of the weights (the effective sample size), the mean
        and scatter are weight-averaged. A weight of 1 everywhere is the plain
        update; a weight of 0 is an observation that is not there. Used so a
        trade from a superseded rule version can count for less than a trade
        from the rule running now (research/versions.py)."""
        x = np.asarray(list(x), dtype=float)
        if weights is None:
            w = np.ones_like(x)
        else:
            w = np.asarray(list(weights), dtype=float)
        m = np.isfinite(x) & np.isfinite(w) & (w > 0)
        d, w = x[m], w[m]
        n = float(w.sum())
        if d.size == 0 or n <= 0:
            return self
        xbar = float((w * d).sum() / n)
        ss = float((w * (d - xbar) ** 2).sum())
        kappa_n = self.kappa + n
        mu_n = (self.kappa * self.mu0 + n * xbar) / kappa_n
        a_n = self.a + n / 2
        b_n = self.b + 0.5 * ss + (self.kappa * n * (xbar - self.mu0) ** 2) / (2 * kappa_n)
        return NormalInverseGamma(mu_n, kappa_n, a_n, b_n)

    @property
    def mean(self) -> float:
        return self.mu0

    @property
    def var_estimate(self) -> float:
        return float(self.b / max(self.a - 1, 1e-9))

    def mean_ci(self, level: float = 0.9) -> tuple[float, float]:
        """Marginal posterior of mu is Student-t."""
        df = 2 * self.a
        scale = math.sqrt(self.b / (self.a * self.kappa))
        alpha = (1 - level) / 2
        t = sps.t.ppf(1 - alpha, df)
        return (self.mu0 - t * scale, self.mu0 + t * scale)

    def prob_above(self, threshold: float) -> float:
        """P(true mean net return > threshold). This is the allocation trigger."""
        df = 2 * self.a
        scale = math.sqrt(self.b / (self.a * self.kappa))
        if scale <= 0:
            return 1.0 if self.mu0 > threshold else 0.0
        return float(1 - sps.t.cdf((threshold - self.mu0) / scale, df))

    def sample_mean(self, rng: np.random.Generator) -> float:
        """Thompson sampling draw."""
        df = 2 * self.a
        scale = math.sqrt(self.b / (self.a * self.kappa))
        return float(self.mu0 + scale * rng.standard_t(df))

    def to_dict(self) -> dict:
        lo, hi = self.mean_ci()
        return {
            "mu0": self.mu0, "kappa": self.kappa, "a": self.a, "b": self.b,
            "mean_bps": self.mu0 * 1e4,
            "mean_ci90_bps": [lo * 1e4, hi * 1e4],
            "sd_estimate_bps": math.sqrt(self.var_estimate) * 1e4,
            "n_effective": self.kappa - 1,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Assumption tests -- shown next to every model so the caveats are visible
# ──────────────────────────────────────────────────────────────────────────────
def assumption_tests(x: Sequence[float]) -> dict:
    """Run the checks whose failure would invalidate the model above it."""
    r = _clean(x)
    n = r.size
    out: dict = {"n": int(n)}
    if n < 20:
        out["note"] = "insufficient sample for assumption testing"
        return out

    jb_stat, jb_p = sps.jarque_bera(r)
    out["normality_jarque_bera"] = {
        "statistic": float(jb_stat), "p_value": float(jb_p),
        "normal_at_5pct": bool(jb_p > 0.05),
        "why_it_matters": "Sharpe-based inference assumes near-normal returns; "
                          "PSR/DSR already correct for skew and kurtosis, so a "
                          "failure here is informative, not fatal.",
    }

    if n > 30:
        lag = min(10, n // 5)
        ac = [float(np.corrcoef(r[:-k], r[k:])[0, 1]) for k in range(1, lag + 1)]
        lb_stat = n * (n + 2) * sum((a**2) / (n - k - 1) for k, a in enumerate(ac))
        lb_p = float(1 - sps.chi2.cdf(lb_stat, lag))
        out["autocorrelation_ljung_box"] = {
            "lags": lag, "statistic": float(lb_stat), "p_value": lb_p,
            "independent_at_5pct": bool(lb_p > 0.05),
            "acf": ac,
            "why_it_matters": "If returns are autocorrelated, iid confidence "
                              "intervals are too narrow. We use a stationary "
                              "block bootstrap to compensate.",
        }

    half = n // 2
    lev_stat, lev_p = sps.levene(r[:half], r[half:])
    out["variance_stability_levene"] = {
        "statistic": float(lev_stat), "p_value": float(lev_p),
        "stable_at_5pct": bool(lev_p > 0.05),
        "why_it_matters": "Unstable variance between the first and second half "
                          "of the sample is the signature of a regime change; "
                          "a strategy fitted on the old regime may be dead.",
    }
    return out
