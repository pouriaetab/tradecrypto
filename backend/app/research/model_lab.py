"""Model Lab -- the full lifecycle of one strategy, in one report.

This is the "designated place" for everything behind a strategy: what data it
saw, how it was split, what was fitted, what the acceptance thresholds are,
which ones it passes, and what would have to change for it to be allowed to
trade. The dashboard renders this object directly; there is no second version of
the truth kept somewhere else.

The split
---------
    |------ train 50% ------|~emb~|-- validation 25% --|~emb~|-- test 25% --|

  train       the ONLY data the calibration sees
  embargo     bars dropped so a position opened just before the boundary cannot
              still be open inside the next block (purging -- Lopez de Prado)
  validation  where you are allowed to look while iterating
  test        looked at once, at the end, and never tuned against

Reporting a test-set number and then going back to change parameters converts
the test set into a training set. The report says this out loud because it is
the single easiest way to fool yourself, and it does not require any bad intent.

The gates
---------
Every gate is a hard, pre-registered threshold with a plain-language reason.
A strategy that fails any gate is not allowed to size up, no matter how good its
equity curve looks. The gates exist precisely so that a good-looking curve is
not sufficient.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from app.config import get_settings
from app.core import db
from app.data import regime as regime_mod
from app.execution import cost_model, symbol_cost
from app.research import backtest as bt
from app.research import stats as S
from app.research.seasonality import calendar_effects
from app.strategy.base import Panel
from app.strategy.registry import STRATEGIES, build, grid


@dataclass
class Gate:
    name: str
    question: str
    threshold: str
    actual: str
    passed: bool
    blocking: bool
    why: str

    def to_dict(self) -> dict:
        return self.__dict__


def _split(T: int, embargo: int) -> dict:
    tr = int(T * 0.50)
    va_start = min(tr + embargo, T - 1)
    va_end = int(T * 0.75)
    te_start = min(va_end + embargo, T - 1)
    return {
        "train": [0, tr],
        "embargo_bars": embargo,
        "validation": [va_start, va_end],
        "test": [te_start, T - 1],
        "note": ("Calibration sees only the train block. The embargo removes bars "
                 "adjacent to each boundary so an open position cannot leak "
                 "information across the split."),
    }


def _perf(res: bt.BTResult, ppy: float, n_trials: int) -> dict:
    s = res.summary(ppy, n_trials=n_trials)
    s["exit_reasons"] = _exit_mix(res)
    return s


def _exit_mix(res: bt.BTResult) -> dict:
    out: dict[str, int] = {}
    for t in res.trades:
        out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
    return out


def run(strategy_name: str, panel: Panel, *,
        cost_bps_per_side: float | None = None,
        include_walk_forward: bool = True) -> dict:
    s_cfg = get_settings()
    cls = STRATEGIES[strategy_name]
    strat = build(strategy_name)
    ppy = 365 * 24 * 3600 / max(panel.bar_seconds(), 1)
    warm = strat.warmup_bars()
    embargo = max(warm // 4, 30)

    span_hours = float((panel.ts[-1] - panel.ts[0]) / 3600) if panel.T > 1 else 0.0
    required_bars = warm * 4
    data_ok = panel.T >= required_bars and span_hours >= 24 * 7

    report: dict = {
        "strategy": strat.describe(),
        "generated_at": time.time(),
        "data": {
            "bars": panel.T, "symbols": panel.N,
            "bar_seconds": panel.bar_seconds(),
            "span_hours": span_hours, "span_days": span_hours / 24,
            "warmup_bars": warm, "required_bars": required_bars,
            "sufficient": data_ok,
            "note": ("Public feeds serve only a few hundred recent candles per call. "
                     "The engine stores every one it fetches, so this grows while it runs."),
        },
    }

    if panel.T < warm + 40:
        report["gates"] = [Gate(
            "data_sufficiency", "Is there enough history to say anything at all?",
            f">= {required_bars} bars and >= 7 days",
            f"{panel.T} bars, {span_hours/24:.1f} days", False, True,
            "Everything downstream is undefined without data. Leave the engine running.",
        ).to_dict()]
        report["verdict"] = "INSUFFICIENT_DATA"
        report["next_action"] = ("Start the engine and let it collect bars. Nothing else in "
                                 "this report can be computed yet.")
        return report

    # ── cost basis ───────────────────────────────────────────────────────────
    if cost_bps_per_side is None:
        per_sym = [symbol_cost.estimate_symbol(sym) for sym in panel.symbols[:60]]
        cost_bps_per_side = float(np.median([c.markup_bps for c in per_sym])) if per_sym \
            else cost_model.estimate().per_side_bps
        cost_source = "median of per-symbol estimates"
    else:
        cost_source = "supplied"
    hurdle = 2 * cost_bps_per_side * s_cfg.cost_safety_multiplier
    report["cost"] = {
        "per_side_bps": cost_bps_per_side, "round_trip_bps": 2 * cost_bps_per_side,
        "round_trip_pct": 2 * cost_bps_per_side / 100,
        "hurdle_bps": hurdle, "hurdle_pct": hurdle / 100, "source": cost_source,
        "note": ("This is what every expected-edge number is compared against. "
                 "It is an estimate until real fills exist for these coins."),
    }

    # ── split and calibration ────────────────────────────────────────────────
    sp = _split(panel.T, embargo)
    report["split"] = sp
    strat.calibrate(panel, sp["train"][1])
    fitted = {k: v for k, v in strat.params.items()
              if k.startswith("beta") or k in ("calib_n", "calib_note", "followers")}
    report["calibration"] = {
        "fitted_on_bars": sp["train"],
        "coefficients": fitted,
        "claims_edge": bool(strat.params.get("beta_bps_per_z")
                            or strat.params.get("beta_bps_per_unit")
                            or strat.params.get("beta_bps_per_rank")),
        "rule": ("A slope whose 95% confidence interval includes zero is set to zero. "
                 "Zero expected edge means the engine will not place a trade, however "
                 "attractive the chart looks."),
    }

    # ── the three passes ─────────────────────────────────────────────────────
    n_trials = max(1, int(np.prod([len(v) for v in grid(strategy_name).values()] or [1])))
    passes = {}
    for label, (a, b) in (("train", sp["train"]), ("validation", sp["validation"]),
                          ("test", sp["test"])):
        res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps_per_side,
                              start_i=max(a, warm + 1), end_i=b,
                              apply_hurdle=True, hurdle_bps=hurdle)
        passes[label] = _perf(res, ppy, n_trials)
    # Diagnostic pass: the same strategy with the cost hurdle switched OFF.
    # When the calibration claims no edge, nothing trades and the table above is
    # empty -- which is correct but tells you nothing about WHY. This shows what
    # the strategy would have done, so you can see the trade count, the exit mix
    # and the shape of the losses. It is a diagnostic, never a performance claim.
    diag = {}
    for label, (a, b) in (("validation", sp["validation"]), ("test", sp["test"])):
        res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps_per_side,
                              start_i=max(a, warm + 1), end_i=b, apply_hurdle=False)
        diag[label] = _perf(res, ppy, n_trials)
    report["diagnostic_no_hurdle"] = diag
    report["diagnostic_note"] = (
        "Cost hurdle disabled. These trades were NOT allowed to happen -- the "
        "calibrated edge did not clear the hurdle, so the engine declined them. "
        "This block exists to show what the signal was pointing at and how it "
        "would have performed, not to argue that it should have traded."
    )

    report["performance"] = passes
    report["performance_note"] = (
        "Train performance is not evidence -- the coefficients were fitted there. "
        "Validation is where you are allowed to iterate. Test is looked at once. "
        "If you change parameters after reading the test row, the test row is no "
        "longer a test."
    )

    # ── cost sensitivity ─────────────────────────────────────────────────────
    report["cost_sensitivity"] = bt.cost_sensitivity(
        panel, strat, [0, 10, 20, 40, 60, 80, 100, 120],
        start_i=max(sp["validation"][0], warm + 1), end_i=sp["test"][1],
    )
    report["cost_sensitivity_note"] = (
        "Read the row nearest the estimated cost above. Where mean_net_bps turns "
        "negative is the break-even cost -- if that number is below Robinhood's "
        "actual spread, this strategy cannot work here regardless of the signal."
    )

    # ── walk-forward ─────────────────────────────────────────────────────────
    wf = None
    if include_walk_forward and panel.T >= warm * 6:
        g = grid(strategy_name)
        if g:
            wf = bt.walk_forward(panel, cls, g, cost_bps_per_side=cost_bps_per_side,
                                 n_folds=5, embargo_bars=embargo)
    report["walk_forward"] = wf

    # ── context ──────────────────────────────────────────────────────────────
    report["regime_now"] = regime_mod.regime_report(panel)
    if strategy_name == "regime_swing":
        report["calendar_effects"] = calendar_effects(panel)

    # ── gates ────────────────────────────────────────────────────────────────
    # `.get(key, default)` returns a STORED None, not the default. walk_forward
    # sets best_out_of_sample = None whenever no configuration produced enough
    # out-of-sample trades -- which is the normal case for a selective strategy
    # like volume_build, whose trades the cost hurdle declines. So the chained
    # .get() raised AttributeError and the whole daily job died with it. `or {}`
    # is the form that survives an explicit None.
    oos = passes.get("test") or {}
    val = passes.get("validation") or {}
    wfd = wf or {}
    best_oos = wfd.get("best_out_of_sample") or {}
    dsr = best_oos.get("deflated_sharpe") if wf else oos.get("deflated_sharpe")
    pbo = wfd.get("pbo") or {}
    curve = report["cost_sensitivity"]
    break_even = next((r["cost_bps_per_side"] for r in curve
                       if r["mean_net_bps"] is not None and r["mean_net_bps"] < 0), None)

    gates = [
        Gate("data_sufficiency", "Is there enough history to say anything?",
             f">= {required_bars} bars and >= 7 days",
             f"{panel.T} bars, {span_hours/24:.1f} days", bool(data_ok), True,
             "Below this, every statistic below is dominated by sampling noise."),
        Gate("calibration_significant",
             "Does the signal predict forward returns on training data?",
             "slope CI excludes zero",
             str(fitted.get("calib_note", "n/a")),
             bool(report["calibration"]["claims_edge"]), True,
             "If the fitted slope is indistinguishable from zero there is no signal to trade."),
        Gate("out_of_sample_trades", "Are there enough held-out trades to judge?",
             ">= 30 on the test block", str(oos.get("n", 0)),
             bool(oos.get("n", 0) >= 30), True,
             "Under about 30 trades a hit rate or Sharpe means essentially nothing."),
        Gate("out_of_sample_profitable",
             "Is the mean net return positive with 95% confidence, after costs?",
             "lower bound of the CI > 0",
             f"{oos.get('mean_return_bps', float('nan')):.1f} bps "
             f"[{(oos.get('mean_return_ci') or [float('nan')])[0]*1e4:.0f}, "
             f"{(oos.get('mean_return_ci') or [0, float('nan')])[1]*1e4:.0f}]",
             bool(oos.get("positive_mean_at_95")), True,
             "A positive average that could easily be zero is not an edge."),
        Gate("deflated_sharpe",
             f"Does it beat what {n_trials} configurations would produce by luck?",
             ">= 0.95",
             f"{dsr:.3f}" if isinstance(dsr, (int, float)) and np.isfinite(dsr) else "n/a",
             bool(isinstance(dsr, (int, float)) and np.isfinite(dsr) and dsr >= 0.95), True,
             "Searching parameters guarantees a best-looking one even when none work."),
        Gate("overfitting_probability",
             "Would picking the in-sample winner beat the median out of sample?",
             f"PBO <= {s_cfg.max_pbo}, measured on >= 8 configurations",
             (f"{pbo.get('pbo'):.2f}" + ("" if pbo.get("reliable") else " (unreliable)")
              if pbo.get("pbo") is not None and np.isfinite(pbo.get("pbo", float("nan"))) else "not measured"),
             bool(pbo.get("reliable") and (pbo.get("pbo") or 1) <= s_cfg.max_pbo), True,
             "Measures whether the selection process itself is the problem."),
        Gate("survives_real_cost",
             "Is the break-even cost above what Robinhood actually charges?",
             f"break-even > {cost_bps_per_side:.0f} bps per side",
             (f"break-even near {break_even:.0f} bps" if break_even is not None
              else ("profitable across the whole tested range"
                    if oos.get("n", 0) + val.get("n", 0) > 0
                    else "no trades generated -- nothing to evaluate")),
             bool(oos.get("n", 0) + val.get("n", 0) > 0
                  and (break_even is None or break_even > cost_bps_per_side)), True,
             "The decisive gate. A real signal that dies below the venue's spread is "
             "a real signal you cannot trade here."),
        Gate("validation_consistency",
             "Do validation and test agree in sign?",
             "same sign of mean net return",
             f"val {val.get('mean_return_bps', float('nan')):.1f} bps, "
             f"test {oos.get('mean_return_bps', float('nan')):.1f} bps",
             bool(np.sign(val.get("mean_return_bps") or 0) == np.sign(oos.get("mean_return_bps") or 0)
                  and (val.get("mean_return_bps") or 0) != 0), False,
             "Disagreement between blocks usually means a regime change, not an edge."),
    ]
    report["gates"] = [g.to_dict() for g in gates]
    blocking_failed = [g for g in gates if g.blocking and not g.passed]
    report["verdict"] = "APPROVED_FOR_PAPER" if not blocking_failed else "NOT_APPROVED"
    report["blocking_failures"] = [g.name for g in blocking_failed]
    report["next_action"] = _next_action(blocking_failed, oos, break_even, cost_bps_per_side)

    db.execute(
        "INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
        (time.time(), "model_lab", strategy_name, str(strat.params)[:8000],
         str({k: report[k] for k in ("verdict", "blocking_failures")})[:8000], report["verdict"]),
    )
    return report


def _next_action(failed: list[Gate], oos: dict, break_even, cost) -> str:
    if not failed:
        return ("Every blocking gate passes. Run it in paper mode until it has the "
                "configured minimum number of live-quote trades, then reconsider real money.")
    names = {g.name for g in failed}
    if "data_sufficiency" in names:
        return "Collect more history. Leave the engine running; nothing else can be judged yet."
    if "calibration_significant" in names:
        return ("The signal does not predict forward returns on training data. Change the "
                "hypothesis rather than the parameters -- tuning a dead signal is how "
                "overfitting starts.")
    if "survives_real_cost" in names and break_even is not None:
        return (f"The strategy breaks even near {break_even:.0f} bps per side but Robinhood "
                f"costs about {cost:.0f} bps. Either find a longer horizon where the move is "
                "bigger relative to the spread, or trade the cheaper coins in the cost table.")
    if "out_of_sample_trades" in names:
        return f"Only {oos.get('n', 0)} held-out trades. Collect more data before judging this."
    return ("Gates failed: " + ", ".join(sorted(names)) +
            ". Do not tune parameters against the test block -- that converts it into "
            "training data and the deflated Sharpe stops protecting you.")
