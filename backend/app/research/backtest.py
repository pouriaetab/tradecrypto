"""Event-driven backtester with realistic execution, plus the validation suite.

Three constraints that most crypto backtests get wrong and this one does not:

1. LONG ONLY. Robinhood does not let you short crypto. A signal that says "fade
   this pump" is not tradeable unless you already hold the coin. The engine
   therefore drops short legs by default and reports separately how much of the
   apparent edge lived in the untradeable half -- a strategy that only works
   with shorts is not a strategy you have.

2. EXECUTION DELAY. A signal computed from the close of bar t is executed at the
   close of bar t+1, never at the price that produced it. Robinhood's agentic
   path is an OAuth MCP round trip mediated by an LLM; a one-bar delay on
   1-minute bars is optimistic if anything.

3. COSTS ON BOTH SIDES, from the measured cost model, with a sensitivity curve
   showing at exactly which cost level the strategy stops making money.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

from app.research import stats as S
from app.strategy.base import Panel, Strategy


@dataclass
class BTTrade:
    symbol: str
    side: str
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    gross_ret: float
    net_ret: float
    exit_reason: str
    expected_edge_bps: float
    raw_score: float


@dataclass
class BTResult:
    trades: list[BTTrade] = field(default_factory=list)
    net_returns: np.ndarray = field(default_factory=lambda: np.array([]))
    params: dict = field(default_factory=dict)
    dropped_short_signals: int = 0
    taken_signals: int = 0
    rejected_below_hurdle: int = 0

    def summary(self, periods_per_year: float, n_trials: int = 1,
                trial_sharpes: list[float] | None = None) -> dict:
        s = S.performance_summary(self.net_returns, periods_per_year, n_trials, trial_sharpes)
        s["dropped_short_signals"] = self.dropped_short_signals
        s["taken_signals"] = self.taken_signals
        s["rejected_below_hurdle"] = self.rejected_below_hurdle
        s["assumption_tests"] = S.assumption_tests(self.net_returns)
        return s


def run_backtest(
    panel: Panel,
    strategy: Strategy,
    *,
    cost_bps_per_side: float,
    long_only: bool = True,
    max_concurrent: int = 3,
    apply_hurdle: bool = True,
    hurdle_bps: float | None = None,
    start_i: int | None = None,
    end_i: int | None = None,
) -> BTResult:
    close = panel.close
    high = panel.high if panel.high is not None else close
    low = panel.low if panel.low is not None else close
    T = panel.T
    lo_i = max(strategy.warmup_bars() + 1, start_i or 0)
    hi_i = min(T - 2, end_i if end_i is not None else T - 2)

    res = BTResult(params=dict(strategy.describe(), cost_bps_per_side=cost_bps_per_side,
                               long_only=long_only, max_concurrent=max_concurrent))
    open_until: dict[str, int] = {}     # symbol -> bar index the position frees up
    sym_index = {s: i for i, s in enumerate(panel.symbols)}
    # (1+s)/(1-s)-1, not 2s. You buy at m(1+s) and sell at m'(1-s), so the mid
    # must rise by slightly MORE than twice the spread because the exit loss is
    # taken on a larger base. At 0.95% a side that is 191.82 bps, not 190. Small
    # per trade, multiplied by every trade forever, and rh_spread already uses the
    # exact form -- the backtester disagreeing with the live cost model is how a
    # strategy gets promoted on numbers the desk cannot reproduce.
    _s = cost_bps_per_side / 1e4
    round_trip = ((1 + _s) / (1 - _s) - 1) * 1e4 if 0 < _s < 0.5 else 2 * cost_bps_per_side
    hurdle = hurdle_bps if hurdle_bps is not None else round_trip

    for t in range(lo_i, hi_i):
        # release finished positions
        open_until = {k: v for k, v in open_until.items() if v > t}
        if len(open_until) >= max_concurrent:
            continue

        for sig in strategy.generate(panel, t):
            if sig.symbol in open_until or len(open_until) >= max_concurrent:
                continue
            if long_only and sig.side == "sell":
                res.dropped_short_signals += 1
                continue
            if apply_hurdle and sig.expected_edge_bps < hurdle:
                res.rejected_below_hurdle += 1
                continue

            i = sym_index[sig.symbol]
            e = t + 1                                  # execution delay: one bar
            entry_ref = close[e, i]
            if not np.isfinite(entry_ref) or entry_ref <= 0:
                continue
            sgn = 1 if sig.side == "buy" else -1
            entry_px = entry_ref * (1 + sgn * cost_bps_per_side / 1e4)

            hold_bars = max(1, int(round(sig.hold_seconds / max(panel.bar_seconds(), 1))))
            stop_px = entry_ref * (1 - sgn * sig.stop_bps / 1e4)
            # No target is a real contract (pump_ride: "a target is what stops a
            # pump rule from working"), and a trailing stop is a stop that
            # follows the running extreme. Until 2026-09-19 this multiplied
            # `None` and the Model Lab reported pump_ride as ERROR every day,
            # so the one strategy whose held-out gross edge is within sight of
            # the spread was the one never graded.
            targ_px = (entry_ref * (1 + sgn * sig.target_bps / 1e4)
                       if sig.target_bps is not None else None)
            trail = float(getattr(sig, "trail_bps", 0.0) or 0.0) / 1e4
            peak = entry_ref

            exit_i, exit_ref, reason = None, None, "time"
            for u in range(e + 1, min(e + 1 + hold_bars, T)):
                hi_u, lo_u = high[u, i], low[u, i]
                if not np.isfinite(hi_u):
                    continue
                if sgn == 1:
                    hit_stop = lo_u <= stop_px
                    hit_targ = targ_px is not None and hi_u >= targ_px
                else:
                    hit_stop = hi_u >= stop_px
                    hit_targ = targ_px is not None and lo_u <= targ_px
                if hit_stop:                            # pessimistic tie-break
                    exit_i, exit_ref, reason = u, stop_px, "stop"
                    break
                if hit_targ:
                    exit_i, exit_ref, reason = u, targ_px, "target"
                    break
                # Ratchet AFTER the check: a bar that prints a new high and then
                # falls through the raised stop is not credited with the exit
                # at that raised stop -- the stop moves for the next bar.
                if trail > 0:
                    if sgn == 1 and hi_u > peak:
                        peak = hi_u
                        stop_px = max(stop_px, peak * (1 - trail))
                    elif sgn == -1 and lo_u < peak:
                        peak = lo_u
                        stop_px = min(stop_px, peak * (1 + trail))
            if exit_i is None:
                exit_i = min(e + hold_bars, T - 1)
                exit_ref = close[exit_i, i]
                reason = "time"
            if not np.isfinite(exit_ref) or exit_ref <= 0:
                continue

            exit_px = exit_ref * (1 - sgn * cost_bps_per_side / 1e4)
            gross = sgn * (exit_ref / entry_ref - 1.0)
            net = sgn * (exit_px / entry_px - 1.0)

            res.trades.append(BTTrade(sig.symbol, sig.side, e, exit_i, entry_px, exit_px,
                                      gross, net, reason, sig.expected_edge_bps, sig.raw_score))
            res.taken_signals += 1
            open_until[sig.symbol] = exit_i

    res.net_returns = np.array([tr.net_ret for tr in res.trades], dtype=float)
    return res


# ──────────────────────────────────────────────────────────────────────────────
def cost_sensitivity(panel: Panel, strategy: Strategy, costs_bps: list[float],
                     **kw) -> list[dict]:
    """Net performance as a function of assumed one-side cost.

    This single curve answers the question that decides the whole project: at
    Robinhood's actual cost, is there anything left? Read the row nearest the
    measured cost, not the row you like.
    """
    out = []
    ppy = 365 * 24 * 3600 / max(panel.bar_seconds(), 1)
    for c in costs_bps:
        r = run_backtest(panel, strategy, cost_bps_per_side=c, apply_hurdle=False, **kw)
        s = r.summary(ppy)
        out.append({
            "cost_bps_per_side": c,
            "round_trip_pct": 2 * c / 100,
            "n_trades": s.get("n", 0),
            "mean_net_bps": s.get("mean_return_bps"),
            "mean_ci_bps": [x * 1e4 for x in s.get("mean_return_ci", [float("nan")] * 2)],
            "hit_rate": s.get("hit_rate"),
            "sharpe_per_trade": s.get("sharpe_per_trade"),
            "profitable_at_95pct_confidence": s.get("positive_mean_at_95", False),
        })
    return out


def walk_forward(
    panel: Panel,
    strategy_cls: type[Strategy],
    param_grid: dict[str, list],
    *,
    cost_bps_per_side: float,
    n_folds: int = 5,
    embargo_bars: int = 60,
    long_only: bool = True,
    max_configs: int = 60,
) -> dict:
    """Purged, embargoed walk-forward with per-configuration OOS returns.

    Purging matters: a trade opened near the train/test boundary can still be
    open inside the test window, leaking information. The embargo drops
    `embargo_bars` immediately after each training block.

    Every configuration tried is COUNTED, and the count is fed to the deflated
    Sharpe ratio. That is the honest accounting most backtests skip.
    """
    keys = list(param_grid)
    combos = [dict(zip(keys, v)) for v in itertools.product(*(param_grid[k] for k in keys))]
    if len(combos) > max_configs:
        step = math.ceil(len(combos) / max_configs)
        combos = combos[::step]
    n_trials = len(combos)

    T = panel.T
    bounds = np.linspace(0, T, n_folds + 1, dtype=int)
    ppy = 365 * 24 * 3600 / max(panel.bar_seconds(), 1)

    per_config: list[dict] = []
    oos_return_streams: list[np.ndarray] = []

    for cfg in combos:
        strat = strategy_cls(**cfg)
        fold_returns: list[float] = []
        fold_reports = []
        for f in range(1, n_folds):
            train_end = int(bounds[f])
            test_start = min(train_end + embargo_bars, T - 2)
            test_end = int(bounds[f + 1])
            if test_end - test_start < strat.warmup_bars() + 10:
                continue
            strat.calibrate(panel, train_end)          # fit on train only
            r = run_backtest(panel, strat, cost_bps_per_side=cost_bps_per_side,
                             long_only=long_only, start_i=test_start, end_i=test_end,
                             apply_hurdle=True, hurdle_bps=2 * cost_bps_per_side)
            fold_returns.extend(r.net_returns.tolist())
            fold_reports.append({
                "fold": f, "train_end": train_end, "test_range": [test_start, test_end],
                "n_trades": len(r.trades),
                "mean_bps": float(np.mean(r.net_returns) * 1e4) if r.net_returns.size else None,
                "fitted_params": {k: v for k, v in strat.params.items() if k.startswith("beta") or k == "calib_n"},
                "dropped_short_signals": r.dropped_short_signals,
                "rejected_below_hurdle": r.rejected_below_hurdle,
            })
        arr = np.array(fold_returns, dtype=float)
        oos_return_streams.append(arr)
        per_config.append({"params": cfg, "n_oos_trades": int(arr.size),
                           "sharpe_per_trade": S.sharpe(arr) if arr.size > 2 else None,
                           "mean_bps": float(arr.mean() * 1e4) if arr.size else None,
                           "folds": fold_reports})

    trial_sharpes = [c["sharpe_per_trade"] for c in per_config
                     if c["sharpe_per_trade"] is not None and np.isfinite(c["sharpe_per_trade"])]

    # Best configuration by OOS mean, evaluated with full selection accounting.
    scored = [(c, s) for c, s in zip(per_config, oos_return_streams) if s.size >= 20]
    best_summary = None
    if scored:
        best_cfg, best_stream = max(scored, key=lambda cs: float(np.mean(cs[1])))
        best_summary = {
            "params": best_cfg["params"],
            **S.performance_summary(best_stream, ppy, n_trials=n_trials, trial_sharpes=trial_sharpes),
        }

    # PBO needs configs evaluated on a common grid; use equal-length truncation.
    pbo = {"pbo": float("nan"), "note": "not enough common observations"}
    usable = [s for s in oos_return_streams if s.size >= 40]
    if len(usable) >= 2:
        m = min(s.size for s in usable)
        X = np.column_stack([s[:m] for s in usable])
        pbo = S.pbo_cscv(X, s=8)
        pbo["n_configurations"] = len(usable)
        # PBO compares configurations against each other. With only a handful of
        # near-identical configurations the ranking is mostly noise and the
        # statistic is not informative -- saying so is better than reporting it.
        pbo["reliable"] = len(usable) >= 8
        if not pbo["reliable"]:
            pbo["note"] = (f"only {len(usable)} configurations produced enough "
                           "out-of-sample trades; PBO needs >= 8 near-independent "
                           "configurations to mean anything. Widen the parameter grid.")

    verdict, reasons = _verdict(best_summary, pbo, n_trials)
    return {
        "n_configurations_tested": n_trials,
        "n_folds": n_folds,
        "embargo_bars": embargo_bars,
        "cost_bps_per_side": cost_bps_per_side,
        "long_only": long_only,
        "per_configuration": per_config,
        "best_out_of_sample": best_summary,
        "pbo": pbo,
        "verdict": verdict,
        "verdict_reasons": reasons,
    }


def _verdict(best: dict | None, pbo: dict, n_trials: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not best:
        return "INSUFFICIENT_EVIDENCE", ["No configuration produced at least 20 out-of-sample trades."]
    n = best.get("n", 0)
    if n < 30:
        reasons.append(f"Only {n} out-of-sample trades; nothing can be concluded below ~30.")
    if not best.get("positive_mean_at_95", False):
        reasons.append("The 95% confidence interval for mean net return includes zero.")
    dsr = best.get("deflated_sharpe")
    if dsr is None or not np.isfinite(dsr) or dsr < 0.95:
        reasons.append(f"Deflated Sharpe {dsr:.3f} < 0.95 after accounting for {n_trials} configurations tested."
                       if dsr is not None and np.isfinite(dsr) else "Deflated Sharpe could not be computed.")
    p = pbo.get("pbo")
    if p is not None and np.isfinite(p) and pbo.get("reliable", False) and p > 0.35:
        reasons.append(f"Probability of backtest overfitting {p:.2f} exceeds the 0.35 ceiling.")
    elif not pbo.get("reliable", False):
        reasons.append("PBO could not be measured reliably (too few configurations); "
                       "this is a gap in the evidence, not a pass.")
    return ("NO_EDGE_DEMONSTRATED" if reasons else "EDGE_SURVIVES_TESTING"), (
        reasons or ["Positive mean net return at 95% confidence, deflated Sharpe above 0.95, PBO within ceiling."]
    )
