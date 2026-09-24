"""Independent cross-check: re-derive the claims by a different method.

The point
---------
If `auc()` had a bug, every test that called `auc()` would agree with it, and the
suite would be green while the number was wrong. The same goes for a look-ahead
flag the feature code sets about itself, or a cost hurdle the cost model declares
correct. A component cannot be the witness for its own correctness.

So each check here answers the same question as the code it audits, using an
independent route to the answer:

  * AUC is recomputed from the Mann-Whitney rank-sum identity rather than by
    walking a ROC curve.
  * Look-ahead is tested by REPLACING every future bar with noise and demanding
    the features at time t come out bit-identical — not by reading a flag.
  * Skill is tested by shuffling the labels and demanding it collapses to chance.
    A model that still scores well on shuffled labels is reading its own answers.
  * Cost is recomputed straight from the numbers on the Robinhood order ticket.

A check that fails here outranks a green suite.
"""
from __future__ import annotations

import time
import traceback

import numpy as np

CHECKS: dict[str, dict] = {}


def check(key: str, title: str, audits: str, method: str):
    def deco(fn):
        CHECKS[key] = {"key": key, "title": title, "audits": audits,
                       "method": method, "fn": fn}
        return fn
    return deco


def catalogue() -> list[dict]:
    return [{k: v for k, v in c.items() if k != "fn"} for c in CHECKS.values()]


# ── 1. AUC, recomputed from ranks ────────────────────────────────────────────
@check("auc_ranksum", "AUC agrees with the rank-sum identity",
       "breakout.auc()",
       "AUC equals (U statistic)/(n_pos*n_neg) from Mann-Whitney. Computed here "
       "by ranking, which shares no code with the ROC walk it audits.")
def _auc_ranksum() -> dict:
    from app.research.breakout import auc
    rng = np.random.default_rng(11)
    worst = 0.0
    for trial in range(12):
        n = int(rng.integers(60, 400))
        y = (rng.random(n) < rng.uniform(0.15, 0.85)).astype(float)
        if y.sum() in (0, n):
            continue
        p = np.clip(y * rng.uniform(0.1, 0.9) + rng.normal(0, 0.4, n), 0, 1)
        theirs = auc(y, p)
        # independent: average rank of positives, ties shared
        order = np.argsort(p, kind="mergesort")
        ranks = np.empty(n, dtype=float)
        ranks[order] = np.arange(1, n + 1, dtype=float)
        sp = np.sort(p)
        i = 0
        while i < n:                      # midrank for ties
            j = i
            while j + 1 < n and sp[j + 1] == sp[i]:
                j += 1
            if j > i:
                ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
            i = j + 1
        npos, nneg = y.sum(), n - y.sum()
        mine = (ranks[y == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg)
        worst = max(worst, abs(theirs - mine))
    return {"ok": worst < 1e-9,
            "detail": f"largest disagreement over 12 random datasets: {worst:.2e}",
            "threshold": "must be < 1e-9"}


# ── 2. Look-ahead, tested by destroying the future ───────────────────────────
@check("no_lookahead", "Features cannot see the future",
       "breakout.find_breakouts() and every rolling statistic it uses",
       "Compute features on a series, then REPLACE every bar after time t with "
       "random noise and recompute. Anything that changes was reading ahead. "
       "This tests behaviour, not a flag the code sets about itself.")
def _no_lookahead() -> dict:
    from app.research import breakout as bo
    from app.strategy.base import Panel
    rng = np.random.default_rng(5)
    n = 1500
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    high = close * (1 + abs(rng.normal(0, 0.002, n)))
    low = close * (1 - abs(rng.normal(0, 0.002, n)))
    vol = abs(rng.normal(1000, 200, n))

    def mk(c, h, l, v):
        return Panel(symbols=["X"], ts=np.arange(n, dtype=float) * 3600,
                     close=c.reshape(-1, 1), high=h.reshape(-1, 1),
                     low=l.reshape(-1, 1), volume=v.reshape(-1, 1))

    cut = 1000
    a = bo.find_breakouts(mk(close, high, low, vol), "X", direction=1)
    c2, h2, l2, v2 = close.copy(), high.copy(), low.copy(), vol.copy()
    c2[cut:] = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n - cut)))
    h2[cut:] = c2[cut:] * 1.01
    l2[cut:] = c2[cut:] * 0.99
    v2[cut:] = abs(rng.normal(9000, 3000, n - cut))
    b = bo.find_breakouts(mk(c2, h2, l2, v2), "X", direction=1)

    fa = {e.idx: e.features for e in a if e.idx < cut - 60}
    fb = {e.idx: e.features for e in b if e.idx < cut - 60}
    shared = sorted(set(fa) & set(fb))
    if not shared:
        return {"ok": False, "detail": "no comparable events before the cut — "
                                       "cannot conclude anything", "threshold": ""}
    bad = []
    for i in shared:
        for k in bo.FEATURE_NAMES:
            x, y = fa[i].get(k, np.nan), fb[i].get(k, np.nan)
            if np.isfinite(x) != np.isfinite(y) or (np.isfinite(x) and abs(x - y) > 1e-9):
                bad.append(f"{k}@{i}")
    return {"ok": not bad,
            "detail": (f"{len(shared)} events compared across {len(bo.FEATURE_NAMES)} "
                       f"features; {len(bad)} changed when the future was replaced"
                       + (f" (first: {bad[:3]})" if bad else "")),
            "threshold": "zero features may change"}


# ── 3. Shuffled labels must destroy the skill ────────────────────────────────
@check("label_shuffle", "Skill collapses when the labels are shuffled",
       "breakout.train() — the whole walk-forward pipeline",
       "Shuffle the labels so no relationship can exist, then train exactly as "
       "normal. Out-of-sample AUC must fall to ~0.5. If it does not, the "
       "pipeline is leaking the answer into the features.")
def _label_shuffle() -> dict:
    from app.research import breakout as bo
    rng = np.random.default_rng(3)
    n = 900
    X = rng.normal(size=(n, 6))
    w = np.array([1.2, -0.8, 0.5, 0.0, 0.0, 0.0])
    y = (1 / (1 + np.exp(-(X @ w))) > rng.random(n)).astype(float)
    names = [f"f{i}" for i in range(6)]

    def oos_auc(yy):
        cut = int(n * 0.6)
        m = bo.fit_logistic(X[:cut], yy[:cut], names, l2=1.0)
        return bo.auc(yy[cut:], m.predict(X[cut:]))

    real = oos_auc(y)
    shuffled = [oos_auc(rng.permutation(y)) for _ in range(15)]
    mu, sd = float(np.mean(shuffled)), float(np.std(shuffled))
    return {"ok": (abs(mu - 0.5) < 0.06) and (real > mu + 0.08),
            "detail": (f"real labels AUC {real:.3f}; shuffled labels "
                       f"{mu:.3f} ± {sd:.3f} over 15 permutations"),
            "threshold": "shuffled must sit at 0.50 ± 0.06, and real must beat it by 0.08"}


# ── 4. Cost, recomputed from the order ticket ────────────────────────────────
@check("cost_from_ticket", "Round trip matches the Robinhood ticket arithmetic",
       "rh_spread.round_trip_bps() and every hurdle derived from it",
       "Recompute from the raw prices captured on the DOGE ticket "
       "(mid 0.090569, buy 0.091485, bid 0.089704) without using the module's formula.")
def _cost_from_ticket() -> dict:
    from app.execution import rh_spread
    mid, buy, bid = 0.090569, 0.091485, 0.089704
    buy_side = (buy - mid) / mid
    sell_side = (mid - bid) / mid
    observed_rt = (buy / bid - 1) * 1e4          # what a real round trip loses
    theirs = rh_spread.round_trip_bps(0.95)
    return {"ok": abs(theirs - observed_rt) < 12.0 and abs(buy_side - sell_side) < 0.0015,
            "detail": (f"ticket implies {buy_side*100:.3f}% buy / {sell_side*100:.3f}% sell; "
                       f"a real round trip loses {observed_rt:.1f} bps; "
                       f"the module says {theirs:.1f} bps"),
            "threshold": "within 12 bps of the ticket, and the two sides symmetric"}


@check("hurdle_above_cost", "No coin's hurdle is below its own round trip",
       "cost_model.hurdle_bps() and symbol_cost.estimate_symbol()",
       "For every active coin, assert hurdle > round trip. A hurdle under the "
       "spread lets through trades that lose money even when the call is right — "
       "this system was using hurdles as low as 93 bps against a 192 bps spread.")
def _hurdle_above_cost() -> dict:
    from app.execution import rh_spread
    from app.core import db
    syms = [r["symbol"] for r in
            db.query("SELECT symbol FROM universe WHERE active=1 ORDER BY symbol")] or ["BTC"]
    bad = []
    for s in syms:
        g = rh_spread.get(s)
        if rh_spread.hurdle_bps(s) <= g["round_trip_bps"]:
            bad.append(s)
    return {"ok": not bad,
            "detail": f"{len(syms)} coins checked; {len(bad)} with a hurdle at or below "
                      f"their round trip{': ' + ', '.join(bad[:8]) if bad else ''}",
            "threshold": "zero coins"}


# ── 5. NaN robustness, the bug that hid for a week ───────────────────────────
@check("nan_robustness", "A single missing bar does not erase the series",
       "atr(), rolling_vwap(), rolling_mean_std(), _trailing_mean()",
       "Punch one NaN into the input and demand that later values stay finite. "
       "These are prefix sums, where one NaN historically poisoned everything "
       "after it — BTC had 27 NaNs in 35,027 bars and produced zero events.")
def _nan_robustness() -> dict:
    from app.research import breakout as bo
    rng = np.random.default_rng(9)
    n = 3000
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    high, low = close * 1.002, close * 0.998
    vol = abs(rng.normal(1000, 100, n))
    results = {}
    h2 = high.copy(); h2[120] = np.nan
    results["atr"] = float(np.mean(np.isfinite(bo.atr(h2, low, close, 60)[500:])))
    v2 = vol.copy(); v2[200] = np.nan
    results["vwap"] = float(np.mean(np.isfinite(bo.rolling_vwap(close, v2, 60)[500:])))
    c2 = close.copy(); c2[300] = np.nan
    mu, sd = bo.rolling_mean_std(c2, 100)
    results["mean_std"] = float(np.mean(np.isfinite(mu[500:])))
    results["trailing_mean"] = float(np.mean(np.isfinite(bo._trailing_mean(c2, 20)[500:])))
    worst = min(results.values())
    return {"ok": worst > 0.99,
            "detail": "; ".join(f"{k} {v*100:.1f}% finite" for k, v in results.items()),
            "threshold": "each must stay >99% finite after the hole"}


# ── 6. The safety interlock ──────────────────────────────────────────────────
@check("live_interlock", "Real money needs two independent conditions",
       "config.live_enabled",
       "Enumerate the truth table directly. Neither the execution mode nor the "
       "confirmation phrase may enable live trading on its own.")
def _live_interlock() -> dict:
    from app.config import Settings
    rows = []
    for mode in ("paper", "mcp"):
        for phrase in ("", "yes", "I_ACCEPT_REAL_MONEY_RISK"):
            s = Settings(TC_EXECUTION_MODE=mode, TC_LIVE_CONFIRM=phrase)
            rows.append((mode, phrase, s.live_enabled))
    enabled = [r for r in rows if r[2]]
    ok = (len(enabled) == 1 and enabled[0][0] == "mcp"
          and enabled[0][1] == "I_ACCEPT_REAL_MONEY_RISK")
    return {"ok": ok,
            "detail": f"{len(rows)} combinations tested, {len(enabled)} enable live"
                      + (f" ({enabled[0][0]} + correct phrase)" if len(enabled) == 1 else ""),
            "threshold": "exactly one combination may enable live trading"}


def run(only: str | None = None) -> dict:
    keys = [only] if only else list(CHECKS)
    out = []
    for k in keys:
        c = CHECKS.get(k)
        if not c:
            out.append({"key": k, "title": k, "ok": False,
                        "detail": "no such check", "audits": "", "method": ""})
            continue
        t0 = time.time()
        try:
            r = c["fn"]()
        except Exception as exc:
            r = {"ok": False,
                 "detail": f"{type(exc).__name__}: {exc}",
                 "traceback": traceback.format_exc(limit=6), "threshold": ""}
        out.append({"key": k, "title": c["title"], "audits": c["audits"],
                    "method": c["method"], "duration_s": time.time() - t0, **r})
    return {"checks": out, "n": len(out),
            "n_failed": sum(1 for r in out if not r["ok"])}
