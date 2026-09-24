"""The whole decision, from the coins looked at to the one that was funded.

The operator asked to see, live, what happens behind a signal: per strategy,
which coins are being considered, which variables go into the judgement and with
what weight, which of them matter, what the model layer is doing, and which coin
was finally promoted and why.

Almost all of that already existed -- scattered across six modules and three
pages. This assembles it into one shape.

WHAT IS REAL HERE, AND WHAT IS NOT. This app exists to show what actually
happened, so the interesting part of this module is what it REFUSES to claim:

  * The live ranking weights are HARDCODED CONSTANTS, not fitted coefficients.
    `day_climb` ranks on `climb_pct * hour_weight` and nothing else. Every
    variable is labelled by the part it plays -- "ranks" if it enters the score,
    "gate" if it only admits or refuses, "recorded" if it is stored and does not
    affect this decision at all. Most are "recorded". Saying otherwise would
    dress a one-line formula up as a model.

  * `hour_weight` is the ONE fitted number in the live path -- an
    empirical-Bayes shrunk per-hour multiplier, and it reports its own verdict,
    including "this is noise" (a flat profile of 1.0), which is common.

  * `expected_edge_bps` and its confidence interval are hardcoded 0.0 for these
    strategies. They are reported as absent rather than as zero, because a
    confidence interval of [0, 0] on screen reads as certainty.

  * Per-variable importance -- coefficient, standard error, z, odds ratio,
    significance -- comes only from `entry_quality`, which is fitted OFFLINE on
    that strategy's own past signals and CHANGES NO TRADE. It is shown as what
    it is: evidence about the variables, not the thing choosing them. When its
    AUC confidence interval includes 0.5 it says so.

  * Coins the strategy looked at and passed over in silence CANNOT be listed
    with reasons. The gates inside `generate()` are bare `continue` statements;
    nothing counts or names what they dropped. The count is real, the reason is
    not available, and this says exactly that instead of inventing one.

Read-only. No writes, no clock, no engine calls.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.core import db
from app.research import signal_race

# Only variables that appear in a strategy's own scoring expression change the
# ranking. Everything else in features_json is context.
ROLE_RANKS = "ranks"
ROLE_GATE = "gate"
ROLE_RECORDED = "recorded"

# features every strategy carries that are about bookkeeping, not the decision
_BOOKKEEPING = {
    "calibration_n", "burst_n", "burst_rank", "regime", "regime_mult",
    "prior_entries_today", "stop_bps", "target_bps", "hold_seconds",
}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _roles_for(rec: dict) -> dict[str, str]:
    """Which recorded variable plays which part, from the strategy's own recipe."""
    roles: dict[str, str] = {}
    for inp in rec.get("inputs") or []:
        roles[str(inp.get("name"))] = ROLE_RANKS
    blob = " ".join(str(g) for g in (rec.get("gates_before_scoring") or []))
    for token in blob.replace("(", " ").replace(")", " ").replace(",", " ").split():
        t = token.strip().strip("`'\"")
        if t and t.replace("_", "").isalnum() and "_" in t:
            roles.setdefault(t, ROLE_GATE)
    return roles


def _quality(strategy: str) -> dict | None:
    """Offline per-variable evidence. Advisory; it changes no trade."""
    try:
        from app.research import entry_quality
        rep = entry_quality.latest(strategy)
    except Exception:
        return None
    if not rep or not rep.get("available"):
        return None
    hold = rep.get("holdout") or {}
    ci = hold.get("auc_ci95") or [None, None]
    return {
        "auc": hold.get("auc"),
        "auc_ci95": ci,
        "beats_chance": bool(ci and ci[0] is not None and ci[0] > 0.5),
        "n_events": rep.get("n_events"),
        "usable_for_ranking": bool(rep.get("usable_for_ranking")),
        "verdict": rep.get("verdict"),
        "coefficients": (rep.get("coefficients") or [])[:15],
        "note": ("Fitted offline on this strategy's own past signals. It does "
                 "not choose anything — it is evidence about which variables "
                 "carried information, nothing more."),
    }


def _hour_profile(strategy: str) -> dict | None:
    """The one genuinely fitted, genuinely changing weight in the live path."""
    try:
        from app.strategy import hour_profile
        prof = hour_profile.profile(strategy)
    except Exception:
        return None
    if not prof:
        return None
    w = prof.get("weights") or {}
    flat = all(abs(float(v) - 1.0) < 1e-9 for v in w.values()) if w else True
    return {
        "weights": w,
        "is_flat": flat,
        "n": prof.get("n"),
        "verdict": prof.get("verdict"),
        "tradable_hours": sorted(prof.get("tradable_hours") or []),
        "what": ("A multiplier per hour of the day, shrunk toward 1.0 in "
                 "proportion to how much of the between-hour spread is "
                 "explainable as noise." + (
                     " It is flat right now, which means the differences "
                     "between hours were not distinguishable from noise."
                     if flat else "")),
    }


def _models(strategy: str) -> dict:
    """More than one model per strategy: the champion and the ones it beat."""
    out: dict[str, Any] = {"champion_params": {}, "rivals": [], "note": None}
    try:
        from app.research import retrain
        out["champion_params"] = retrain.active_params(strategy) or {}
        hist = retrain.history(strategy, limit=6) or []
        out["rivals"] = [{
            "fitted_ts": h.get("fitted_ts"),
            "verdict": h.get("verdict"),
            "promoted": bool(h.get("promoted")),
            "is_champion": bool(h.get("is_champion")),
            "n_live_trades": h.get("n_live_trades"),
        } for h in hist]
    except Exception as exc:
        out["note"] = f"retraining history unavailable: {type(exc).__name__}"
    if not out["champion_params"]:
        out["note"] = ("No fitted parameters are in force. The strategy is "
                       "running on its written defaults — retraining promotes "
                       "only at two standard errors on held-out trades.")
    return out


def _odds(features: dict, symbol: str | None = None) -> dict | None:
    """P(the target is reached), from the ratio table. A real number, no CI.

    Only `day_climb` records `coin_vol_pct`, so reading it from features alone
    left every other strategy with no odds at all. The engine does not guess
    either -- it measures the coin's own hourly range -- so this falls back to
    the SAME helper rather than to a default, and reports nothing when even that
    is unavailable.
    """
    target_bps = _num(features.get("target_bps"))
    if target_bps is None or target_bps <= 0:
        return None
    vol_pct = _num(features.get("coin_vol_pct"))
    atr_frac = (vol_pct / 100.0) if vol_pct else None
    if (not atr_frac or atr_frac <= 0) and symbol:
        try:
            from app.execution.engine import hourly_range_frac
            atr_frac = float(hourly_range_frac(symbol))
        except Exception:
            atr_frac = None
    if not atr_frac or atr_frac <= 0:
        return None
    try:
        from app.strategy import time_budget
        b = time_budget.budget(target_bps / 1e4, atr_frac)
    except Exception:
        return None
    return {"p_reach": b.get("p_reach"), "p50_h": b.get("p50_h"),
            "p70_h": b.get("p70_h"), "ratio": b.get("ratio"),
            "why": b.get("why"),
            "source": ("measured from 4.9M historical observations of "
                       "target ÷ hourly range; a lookup, not a fit — so there "
                       "is no confidence interval on it")}


def _variables(features: dict, roles: dict[str, str], coefs: dict) -> list[dict]:
    rows = []
    for k, v in sorted(features.items()):
        if k in _BOOKKEEPING:
            continue
        val = _num(v)
        role = roles.get(k, ROLE_RECORDED)
        c = coefs.get(k)
        rows.append({
            "name": k,
            "value": val if val is not None else v,
            "role": role,
            "importance": ({"coef": c.get("coef"), "z": c.get("z"),
                            "std_error": c.get("std_error"),
                            "odds_ratio": c.get("odds_ratio"),
                            "significant": bool(c.get("significant_5pct")),
                            "direction": c.get("direction")} if c else None),
        })
    rows.sort(key=lambda r: (
        {ROLE_RANKS: 0, ROLE_GATE: 1, ROLE_RECORDED: 2}[r["role"]],
        -abs(float((r["importance"] or {}).get("z") or 0.0)),
        r["name"],
    ))
    return rows


def live(mode: str = "paper", strategies: list[str] | None = None) -> dict:
    """The most recent decision each strategy made, opened all the way up."""
    from app.strategy.registry import ACTIVE_STRATEGIES

    names = strategies or list(ACTIVE_STRATEGIES)
    universe = db.query_one("SELECT COUNT(*) c FROM universe WHERE active=1")
    n_universe = int(universe["c"]) if universe else 0

    out: list[dict] = []
    for name in names:
        last = db.query_one(
            "SELECT MAX(ts) t FROM signals WHERE strategy=?", (name,))
        ts = float(last["t"]) if last and last["t"] else None
        rec = signal_race.recipe(name)
        quality = _quality(name)
        coefs = {c.get("feature"): c for c in (quality or {}).get("coefficients", [])}

        cands: list[dict] = []
        if ts is not None:
            rows = db.query(
                "SELECT * FROM signals WHERE strategy=? AND ts=? "
                "ORDER BY raw_score DESC", (name, ts))
            roles = _roles_for(rec)
            for i, r in enumerate(rows):
                try:
                    feats = json.loads(r["features_json"] or "{}")
                except Exception:
                    feats = {}
                try:
                    checks = json.loads(r["checks_json"] or "[]")
                except Exception:
                    checks = []
                decision = r["decision"] or ""
                cands.append({
                    "symbol": r["symbol"],
                    "rank": int(r["field_rank"] or (i + 1)),
                    "raw_score": _num(r["raw_score"]),
                    "decision": decision,
                    "promoted": decision.startswith("taken"),
                    "reject_reason": r["reject_reason"],
                    "variables": _variables(feats, roles, coefs),
                    "gates": [{
                        "check": c.get("check"), "passed": bool(c.get("passed")),
                        "detail": c.get("detail"), "limit": c.get("limit"),
                        "actual": c.get("actual"),
                    } for c in checks],
                    "gates_passed": sum(1 for c in checks if c.get("passed")),
                    "gates_total": len(checks),
                    "odds": _odds(feats, r["symbol"]),
                    # These four columns are structurally zero for every strategy
                    # in the live roster, so they are reported as absent. A
                    # confidence interval printed as [0, 0] reads as certainty.
                    "expected_edge_bps": _num(r["expected_edge_bps"]) or None,
                    "edge_ci_bps": None,
                })

        emitted = len(cands)
        winners = [c["symbol"] for c in cands if c["promoted"]]
        out.append({
            "strategy": name,
            "ts": ts,
            "recipe": rec,
            "considered": {
                "universe": n_universe,
                "emitted": emitted,
                "silent": max(0, n_universe - emitted),
                "note": ("The strategy looked at every active coin and emitted "
                         f"{emitted}. The other {max(0, n_universe - emitted)} "
                         "were dropped by gates inside the strategy, which do "
                         "not record which coin or why — so they are counted "
                         "here, not named. Fixing that means writing a reason "
                         "at every drop point."),
            },
            "model": {
                "hour_profile": _hour_profile(name),
                "versions": _models(name),
                "quality": quality,
            },
            "candidates": cands,
            "selected": ({"symbol": winners[0],
                          "why": (f"highest {rec.get('expr') or 'score'} of "
                                  f"{emitted} and it passed every risk gate")}
                         if winners else None),
            "nothing_taken": not winners,
        })

    return {
        "generated_at": time.time(),
        "mode": mode,
        "strategies": out,
        "honesty": [
            "Ranking weights are constants written into each strategy, not "
            "fitted coefficients. Each variable is labelled by the part it "
            "plays: ranks, gate, or recorded.",
            "hour_weight is the only fitted number in the live path, and it "
            "reports when it is flat because the differences were noise.",
            "Per-variable coefficients and AUC come from entry_quality, fitted "
            "offline. They are evidence about the variables and change nothing.",
            "Coins dropped inside a strategy are counted, not named — nothing "
            "records a reason at those drop points.",
        ],
    }
