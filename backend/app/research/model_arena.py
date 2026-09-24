"""Several models per strategy, competing on held-out data, live.

The operator's objection, and it was a fair one: a decision tree that says "the
weights are constants" describes what the app does, not what he asked for. He
asked for the real thing -- variables competing for influence, more than one
model per strategy competing against each other on AUC or Brier, a train and
validate split, the winner's variable importances, and the winner then applied
to today's coins to rank them.

So this builds it. What it will not do is pretend it has evidence it does not.

THE DESIGN

  Dataset   Every past signal of this strategy, joined to what the trade it
            became actually did. Outcome = the trade made money after costs.
            A signal that never became a trade has no outcome and is dropped.

  Split     BY TIME, never at random. Shuffling a time series lets the model
            learn from an afternoon to predict that morning, which inflates
            every number that follows. The oldest 70% trains, the newest 30%
            is held out and read exactly once.

  Entrants  Five, including two deliberate controls:
              all          every recorded variable, mild regularisation
              regularised  the same variables, heavy regularisation -- wins
                           when the wide model is memorising
              top_k        refit on the five strongest variables only
              single_best  the strongest single variable
              base_rate    the base rate, no variables at all
            `base_rate` exists so a champion has something to beat. A model
            that cannot beat it has learned nothing, and without it in the
            table that fact is invisible.

  Judged on AUC on the held-out slice, with a bootstrap confidence interval,
            plus Brier and log loss. The champion is the best AUC whose
            interval clears 0.50 -- if none does, there IS no champion, and
            that is reported rather than papered over by ranking noise.

  Applied   The champion scores today's candidates: a probability per coin,
            ranked. Advisory. It does not place or block anything, and the tree
            says so, because a number on screen that looks like it is steering
            trades had better be steering them.

Nothing here writes to the trade tables. Read, fit, report.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any

import numpy as np

from app.core import db

TRAIN_FRACTION = 0.70
MIN_TRADES = 40            # below this nothing is fitted at all
ROWS_PER_FEATURE = 8       # a 10-variable model wants ~80 outcomes
TOP_K = 5
BOOTSTRAP_DRAWS = 300
MIN_HOLDOUT = 12

# Stored with the decision but not a property of the setup being judged.
_SKIP = {
    "demo", "burst_n", "burst_rank", "prior_entries_today", "calibration_n",
    "hold_seconds", "regime_mult", "p_reach", "p70_hours",
}


def _rows(strategy: str, limit: int = 4000) -> list[dict]:
    """Signals joined to the outcome of the trade they became."""
    return db.query(
        """
        SELECT s.ts, s.symbol, s.features_json,
               t.net_pnl_usd, t.qty, t.entry_px
          FROM signals s
          JOIN trades  t
            ON t.symbol = s.symbol AND t.strategy = s.strategy
           AND t.ts_open >= s.ts - 3600 AND t.ts_open <= s.ts + 7200
         WHERE s.strategy = ? AND s.decision LIKE 'taken%'
         ORDER BY s.ts ASC
         LIMIT ?
        """,
        (strategy, limit),
    )


def _matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str], list[float]]:
    feats: list[dict] = []
    ys: list[float] = []
    ts: list[float] = []
    for r in rows:
        try:
            f = json.loads(r["features_json"] or "{}")
        except Exception:
            continue
        clean = {}
        for k, v in f.items():
            if k in _SKIP:
                continue
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(x):
                clean[k] = x
        if not clean:
            continue
        feats.append(clean)
        ys.append(1.0 if float(r["net_pnl_usd"] or 0.0) > 0 else 0.0)
        ts.append(float(r["ts"]))

    if not feats:
        return np.zeros((0, 0)), np.zeros(0), [], []

    # Only variables present on effectively every row. A column that is missing
    # half the time is imputed noise, and imputed noise is what makes a wide
    # model look clever on the training slice.
    counts: dict[str, int] = {}
    for f in feats:
        for k in f:
            counts[k] = counts.get(k, 0) + 1
    names = sorted(k for k, c in counts.items() if c >= 0.95 * len(feats))
    if not names:
        return np.zeros((0, 0)), np.zeros(0), [], []

    X = np.array([[f.get(n, 0.0) for n in names] for f in feats], dtype=float)
    return X, np.array(ys, dtype=float), names, ts


def _auc_ci(y: np.ndarray, p: np.ndarray, draws: int = BOOTSTRAP_DRAWS) -> list[float]:
    from app.research.breakout import auc
    rng = np.random.default_rng(7)
    n = len(y)
    if n < 8:
        return [float("nan"), float("nan")]
    vals = []
    for _ in range(draws):
        idx = rng.integers(0, n, n)
        if len(set(y[idx].tolist())) < 2:
            continue
        vals.append(auc(y[idx], p[idx]))
    if len(vals) < 20:
        return [float("nan"), float("nan")]
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def _score(y: np.ndarray, p: np.ndarray) -> dict:
    from app.research.breakout import auc, brier, log_loss
    ci = _auc_ci(y, p)
    a = float(auc(y, p))
    return {
        "auc": a,
        "auc_ci95": ci,
        "brier": float(brier(y, p)),
        "log_loss": float(log_loss(y, p)),
        "beats_chance": bool(ci[0] == ci[0] and ci[0] > 0.5),
    }


def _entrants(Xtr, ytr, names) -> list[dict]:
    """Fit every competitor on the TRAINING slice only."""
    from app.research.breakout import fit_logistic

    out: list[dict] = []

    def add(key, label, why, model, cols):
        out.append({"key": key, "label": label, "why": why,
                    "model": model, "columns": cols})

    try:
        full = fit_logistic(Xtr, ytr, names, l2=1.0)
        add("all", "every variable",
            "All recorded variables, lightly regularised. The widest model — it "
            "wins when there really is signal spread across many of them.",
            full, list(names))
    except Exception:
        full = None

    if full is not None:
        try:
            add("regularised", "every variable, held back",
                "The same variables with heavy regularisation, which shrinks "
                "coefficients toward zero. It beats the wide model exactly when "
                "the wide model was memorising the training slice.",
                fit_logistic(Xtr, ytr, names, l2=10.0), list(names))
        except Exception:
            pass

        ranked = sorted(full.coefficients(), key=lambda c: -abs(c.get("z") or 0.0))
        top = [c["feature"] for c in ranked[:TOP_K] if c["feature"] in names]
        if len(top) >= 2:
            idx = [names.index(t) for t in top]
            try:
                add("top_k", f"strongest {len(top)}",
                    "Refitted on only the strongest variables. Fewer things to "
                    "fit means less room to overfit, which usually shows up as a "
                    "better held-out score than the wide model.",
                    fit_logistic(Xtr[:, idx], ytr, top, l2=1.0), top)
            except Exception:
                pass
        if top:
            one = top[0]
            i = names.index(one)
            try:
                add("single_best", f"just {one}",
                    "One variable, alone. If this matches the others, the rest "
                    "are decoration.",
                    fit_logistic(Xtr[:, [i]], ytr, [one], l2=1.0), [one])
            except Exception:
                pass

    add("base_rate", "no variables at all",
        "Predicts the same number for every coin — how often this strategy wins "
        "overall. THE CONTROL. Any model that cannot beat it has learned "
        "nothing, however good its other numbers look.",
        None, [])
    return out


def run(strategy: str) -> dict:
    """Fit the field, judge it on held-out data, and name a champion if there is one."""
    t0 = time.time()
    rows = _rows(strategy)
    X, y, names, ts = _matrix(rows)
    n = len(y)

    base = {"strategy": strategy, "n_outcomes": n,
            "n_variables": len(names), "variables": names,
            "took_s": round(time.time() - t0, 2)}

    if n < MIN_TRADES:
        return {**base, "available": False,
                "why": (f"{n} finished trades with recorded variables. Fitting "
                        f"anything below {MIN_TRADES} produces a number, not "
                        f"evidence — the model would be describing this "
                        f"particular fortnight."),
                "need": MIN_TRADES, "entrants": [], "champion": None}

    want = max(MIN_TRADES, len(names) * ROWS_PER_FEATURE)
    thin = n < want

    cut = int(n * TRAIN_FRACTION)
    Xtr, ytr, Xho, yho = X[:cut], y[:cut], X[cut:], y[cut:]
    if len(yho) < MIN_HOLDOUT or len(set(yho.tolist())) < 2:
        return {**base, "available": False,
                "why": (f"the held-out slice has {len(yho)} trades and they are "
                        "not mixed enough to score against — with every outcome "
                        "the same, every model scores identically."),
                "need": MIN_TRADES, "entrants": [], "champion": None}

    entrants = []
    for e in _entrants(Xtr, ytr, names):
        if e["model"] is None:
            p = np.full(len(yho), float(ytr.mean()))
        else:
            cols = [names.index(c) for c in e["columns"]]
            p = e["model"].predict(Xho[:, cols])
        sc = _score(yho, p)
        entrants.append({
            "key": e["key"], "label": e["label"], "why": e["why"],
            "n_variables": len(e["columns"]), "variables": e["columns"],
            **sc,
            "coefficients": (e["model"].coefficients()[:12] if e["model"] else []),
        })

    entrants.sort(key=lambda r: (-(r["auc"] if r["auc"] == r["auc"] else 0), r["brier"]))
    winners = [e for e in entrants if e["beats_chance"]]
    champ = winners[0] if winners else None
    for e in entrants:
        e["is_champion"] = bool(champ and e["key"] == champ["key"])

    return {
        **base,
        "available": True,
        "thin": thin,
        "n_train": int(cut), "n_holdout": int(len(yho)),
        "base_rate": float(y.mean()),
        "split": ("oldest 70% to train, newest 30% held out and read once. "
                  "Split by time, never shuffled — shuffling would let it learn "
                  "from an afternoon to predict that morning."),
        "entrants": entrants,
        "champion": champ,
        "verdict": (
            f"{champ['label']} leads with AUC {champ['auc']:.3f} "
            f"(95% CI {champ['auc_ci95'][0]:.3f}–{champ['auc_ci95'][1]:.3f}) "
            f"on {len(yho)} held-out trades."
            if champ else
            "No model beat a coin flip on held-out data. There is no champion, "
            "and ranking the entrants against each other here would be ranking "
            "noise."
        ) + (
            f" Thin: {n} outcomes for {len(names)} variables, which wants about "
            f"{want}. Treat every number here as provisional." if thin else ""
        ),
        "applies_to_trading": False,
        "note": ("Advisory. Nothing here places, blocks or re-ranks a trade — "
                 "the live ranking is still each strategy's own written "
                 "expression. This is the evidence for changing that, not the "
                 "change."),
    }


def rank_candidates(strategy: str, candidates: list[dict]) -> dict | None:
    """Score today's coins with the champion, if there is one."""
    rep = run(strategy)
    champ = rep.get("champion")
    if not champ or not candidates:
        return {"report": rep, "ranked": None}

    from app.research.breakout import fit_logistic
    rows = _rows(strategy)
    X, y, names, _ = _matrix(rows)
    cols = [c for c in champ["variables"] if c in names]
    if not cols:
        return {"report": rep, "ranked": None}
    idx = [names.index(c) for c in cols]
    try:
        model = fit_logistic(X[:, idx], y, cols, l2=1.0)
    except Exception:
        return {"report": rep, "ranked": None}

    out = []
    for c in candidates:
        vals = {v["name"]: v.get("value") for v in (c.get("variables") or [])}
        try:
            row = np.array([[float(vals.get(k, 0.0)) for k in cols]], dtype=float)
            p = float(model.predict(row)[0])
        except Exception:
            continue
        out.append({"symbol": c.get("symbol"), "p_win": p,
                    "promoted_by_strategy": bool(c.get("promoted"))})
    out.sort(key=lambda r: -r["p_win"])
    for i, r in enumerate(out):
        r["model_rank"] = i + 1
    return {
        "report": rep, "ranked": out,
        "threshold": rep.get("base_rate"),
        "how_to_read": ("Probability this setup ends in profit after costs, from "
                        "the champion above. The threshold is the strategy's own "
                        "base rate: above it, this model thinks the setup is "
                        "better than average for this strategy. It is NOT what "
                        "chose the trade — compare it with the ranking that did."),
    }
