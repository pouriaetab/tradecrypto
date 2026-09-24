"""Strategy versions: every trade knows the exact rule that produced it, and
the learning loop knows how much an old version's trades should count.

    "let's version them so ... we are able to compare against previous
     versions as well or to isolate the latest version to train test validate
     and evaluate on ... give more weight to the new data rather than old data
     from the older version ... but if we got to the point that the versions
     are good and we are just tuning here and there ... factor that in"

THREE THINGS, in one module
---------------------------
1. TAGGING. A trade is stamped at close with `strategy_version` (the class
   version string) and `params_hash` (a hash of the parameters actually in
   force, promoted overrides included). Same version string, different
   promoted parameters = different hash = different rule. Without this
   there is nothing to compare or to weight; with it, every question below
   is a GROUP BY.

2. WEIGHTING. When the learning loop reads a strategy's trades, each trade
   is weighted by how much the rule that made it resembles the rule running
   now:

        weight = lambda ** (versions since)          lambda in [0, 1]

   and lambda is NOT typed in. It is the share of parameters the two rules
   have in common (a tuning that moved one parameter of twelve is ~0.92 --
   its trades still say nearly everything about today's rule; a rewrite that
   changed most of them is ~0.2 -- its trades say little). Trades of a
   version that has been REMOVED get weight 0: history, not training data.
   The weighted posterior is exact (weighted conjugate update), and the
   effective sample size it reports is the sum of the weights, so a
   strategy with 60 trades of which 40 are from a rewritten version shows
   n_eff ~ 28, not 60.

3. THE STABILITY QUESTION -- "how do we know we are past the rewrites and
   just tuning?" Three measurements, each from tables the desk already
   writes, each with the number that would make it true:

   (a) promotion rate     promotions per 100 closed trades over the last
                          window. Rewrites promote often; a settled rule
                          does not. Stable when the recent rate is 0.
   (b) parameter drift    the median relative change in each parameter over
                          the last K refits, against the walk-forward's own
                          resolution (the grid step). Stable when the drift
                          is smaller than one grid step -- the fitter is
                          moving inside its own noise.
   (c) out-of-sample      whether the champion's held-out net per trade has
       consistency        stayed inside its own earlier confidence interval
                          for the last M refits. Stable when it has.

   When all three hold, version weighting is switched off (every version's
   trades count in full) and only TIME decay remains, with a half-life
   estimated from the autocorrelation of monthly performance -- a series
   that forgets itself in two months gets a two-month half-life; one that
   does not gets none. The report says which of the three hold, so the
   switch is a reading, not an opinion.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

import numpy as np

from app.core import db

# Parameter keys that describe the fit rather than the rule; excluded from the hash
_NOT_RULE = {"calib_n", "calib_note", "expected_edge_bps"}


# ───────────────────────────── 1. tagging ────────────────────────────────────

def ensure_schema() -> None:
    for col in ("strategy_version", "params_hash"):
        try:
            db.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT")
        except Exception:
            pass
    try:                                    # the refit ledger this module reads
        from app.research import retrain
        retrain.ensure_schema()
    except Exception:
        pass


def params_hash(params: dict) -> str:
    clean = {k: v for k, v in (params or {}).items() if k not in _NOT_RULE}
    blob = json.dumps(clean, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def current(strategy: str) -> dict:
    """The rule in force right now: class version + hash of live parameters."""
    from app.strategy.registry import build
    try:
        from app.research import retrain
        overrides = retrain.active_params(strategy)
    except Exception:
        overrides = {}
    st = build(strategy, **(overrides or {}))
    return {"strategy": strategy, "version": str(getattr(st, "version", "?")),
            "params_hash": params_hash(st.params), "params": st.params}


def tag_trade(trade_id: int, strategy: str) -> dict:
    """Stamp a freshly closed trade. Called by the engine at close."""
    ensure_schema()
    cur = current(strategy)
    db.execute("UPDATE trades SET strategy_version=?, params_hash=? WHERE id=?",
               (cur["version"], cur["params_hash"], int(trade_id)))
    return cur


def backfill_untagged() -> int:
    """Older trades: the version from the signal that opened them, the hash of
    the rule as it stands now (labelled with a * so nobody mistakes it for a
    recorded fact)."""
    ensure_schema()
    rows = db.query("SELECT id, strategy FROM trades WHERE params_hash IS NULL")
    n = 0
    for r in rows:
        ver = db.query_one(
            "SELECT s.strategy_version v FROM trades t JOIN orders o ON o.id = t.open_order_id "
            "JOIN signals s ON s.id = o.signal_id WHERE t.id=?", (r["id"],))
        try:
            cur = current(r["strategy"])
            h = cur["params_hash"] + "*"
            v = (ver["v"] if ver and ver["v"] else cur["version"])
        except Exception:
            h, v = "unknown*", (ver["v"] if ver and ver["v"] else "?")
        db.execute("UPDATE trades SET strategy_version=?, params_hash=? WHERE id=?", (v, h, r["id"]))
        n += 1
    return n


# ──────────────────────────── 2. weighting ───────────────────────────────────

def similarity(a: dict | None, b: dict | None) -> float:
    """Share of rule parameters two versions have in common (0..1)."""
    if not a or not b:
        return 0.0
    ka = {k: v for k, v in a.items() if k not in _NOT_RULE}
    kb = {k: v for k, v in b.items() if k not in _NOT_RULE}
    keys = set(ka) | set(kb)
    if not keys:
        return 1.0
    same = sum(1 for k in keys if k in ka and k in kb and ka[k] == kb[k])
    return same / len(keys)


def _version_chain(strategy: str) -> list[dict]:
    """Every distinct rule this strategy has traded under, oldest first, with
    the parameters where they are known (the retrain table records them;
    the very first rule is the class defaults)."""
    ensure_schema()
    rows = db.query(
        "SELECT strategy_version, params_hash, MIN(ts_close) first_ts, MAX(ts_close) last_ts, "
        "COUNT(*) n FROM trades WHERE strategy=? AND params_hash IS NOT NULL "
        "GROUP BY strategy_version, params_hash ORDER BY MIN(ts_close)", (strategy,))
    known: dict[str, dict] = {}
    try:
        from app.strategy.registry import build
        base = build(strategy).params
        known[params_hash(base)] = base
        for mv in db.query("SELECT params_json FROM model_versions WHERE strategy=?", (strategy,)):
            try:
                p = {**base, **json.loads(mv["params_json"] or "{}")}
                known[params_hash(p)] = p
            except Exception:
                continue
    except Exception:
        pass
    out = []
    for r in rows:
        h = (r["params_hash"] or "").rstrip("*")
        out.append({"version": r["strategy_version"], "params_hash": r["params_hash"],
                    "first_ts": r["first_ts"], "last_ts": r["last_ts"], "n": r["n"],
                    "params": known.get(h)})
    return out


def weights_for(strategy: str, trade_rows: list[dict], stable: bool | None = None) -> np.ndarray:
    """One weight per trade row (needs 'params_hash'), against the current rule.

    stable=True switches version weighting off (all 1.0) -- the report below
    decides that; the caller may pass it to avoid recomputing."""
    cur = current(strategy)
    chain = _version_chain(strategy)
    if stable is None:
        stable = stability(strategy, chain=chain).get("stable", False)
    active = {v["params_hash"].rstrip("*"): i for i, v in enumerate(chain)}
    cur_i = active.get(cur["params_hash"], len(chain))
    w = np.ones(len(trade_rows), dtype=float)
    if stable:
        return w
    lam_cache: dict[str, float] = {}
    for i, r in enumerate(trade_rows):
        h = (r.get("params_hash") or "").rstrip("*")
        if not h or h == cur["params_hash"]:
            continue
        if h not in active:
            w[i] = 0.0                              # a removed rule: history
            continue
        if h not in lam_cache:
            prev = next((v for v in chain if v["params_hash"].rstrip("*") == h), None)
            lam_cache[h] = similarity(prev.get("params") if prev else None, cur["params"])
        dist = max(1, cur_i - active[h])
        w[i] = lam_cache[h] ** dist
    return w


# ──────────────────────────── 3. stability ───────────────────────────────────

def _grid_step(strategy: str, key: str) -> float | None:
    """The walk-forward's own resolution for a parameter: the smallest relative
    step in its grid. Drift below this is the fitter moving inside its noise."""
    try:
        from app.strategy.registry import retrain_grid, grid
        vals = sorted({float(v) for v in (retrain_grid(strategy).get(key) or grid(strategy).get(key) or [])
                       if isinstance(v, (int, float))})
    except Exception:
        return None
    steps = [abs(b - a) / max(abs(a), 1e-9) for a, b in zip(vals, vals[1:]) if a]
    return min(steps) if steps else None


def stability(strategy: str, window_trades: int = 100, k_refits: int = 5,
              m_holdouts: int = 3, chain: list[dict] | None = None) -> dict:
    """The three readings, with the numbers behind them."""
    ensure_schema()
    chain = chain if chain is not None else _version_chain(strategy)
    refits = db.query(
        "SELECT fitted_ts, params_json, holdout_json, promoted, is_champion FROM model_versions "
        "WHERE strategy=? ORDER BY fitted_ts", (strategy,))
    n_trades = db.query_one("SELECT COUNT(*) c FROM trades WHERE strategy=?", (strategy,))["c"]
    recent = db.query("SELECT ts_close FROM trades WHERE strategy=? ORDER BY ts_close DESC LIMIT ?",
                      (strategy, window_trades))
    t_from = float(recent[-1]["ts_close"]) if recent else 0.0

    # (a) promotions per 100 trades in the recent window
    promos = [r for r in refits if r["promoted"] and float(r["fitted_ts"]) >= t_from]
    rate = (100.0 * len(promos) / len(recent)) if recent else None
    a_ok = bool(recent) and rate == 0.0

    # (b) parameter drift over the last K refits vs the champion, in grid steps
    try:
        from app.strategy.registry import build
        champ = build(strategy).params
        try:
            from app.research import retrain
            champ = {**champ, **(retrain.active_params(strategy) or {})}
        except Exception:
            pass
    except Exception:
        champ = {}
    drift_rows = []
    for r in refits[-k_refits:]:
        try:
            p = json.loads(r["params_json"] or "{}")
        except Exception:
            continue
        for key, v in p.items():
            c = champ.get(key)
            if isinstance(v, (int, float)) and isinstance(c, (int, float)) and c:
                rel = abs(float(v) - float(c)) / abs(float(c))
                step = _grid_step(strategy, key)
                drift_rows.append({"param": key, "rel_change": rel, "grid_step": step,
                                   "in_steps": (rel / step) if step else None})
    in_steps = [d["in_steps"] for d in drift_rows if d["in_steps"] is not None]
    drift_med = float(np.median(in_steps)) if in_steps else None
    b_ok = drift_med is not None and drift_med <= 1.0

    # (c) held-out net staying inside its own earlier CI for the last M refits
    hold = []
    for r in refits:
        try:
            h = json.loads(r["holdout_json"] or "{}")
            net = h.get("net_per_trade_pct", h.get("mean_net_pct"))
            se = h.get("se_pct", h.get("se"))
            if net is not None:
                hold.append({"ts": r["fitted_ts"], "net": float(net),
                             "se": float(se) if se is not None else None})
        except Exception:
            continue
    inside = []
    for prev, curr in zip(hold, hold[1:]):
        if prev["se"]:
            inside.append(abs(curr["net"] - prev["net"]) <= 1.96 * prev["se"])
    recent_inside = inside[-m_holdouts:]
    c_ok = len(recent_inside) >= m_holdouts and all(recent_inside)

    # time-decay half-life from the autocorrelation of monthly performance
    months = db.query(
        "SELECT strftime('%Y-%m', ts_close, 'unixepoch') m, "
        "SUM(net_pnl_usd)/SUM(qty*entry_px)*100 net FROM trades WHERE strategy=? "
        "GROUP BY m ORDER BY m", (strategy,))
    series = np.array([float(r["net"]) for r in months if r["net"] is not None], dtype=float)
    half_life_months = None
    if series.size >= 6:
        x = series - series.mean()
        rho1 = float((x[:-1] * x[1:]).sum() / max((x * x).sum(), 1e-12))
        if 0 < rho1 < 1:
            half_life_months = -math.log(2) / math.log(rho1)   # AR(1): rho^h = 1/2
    stable = bool(a_ok and b_ok and c_ok)
    return {
        "strategy": strategy, "n_trades": n_trades, "n_versions_traded": len(chain),
        "n_refits": len(refits), "stable": stable,
        "a_promotion_rate_per_100": {"value": rate, "window_trades": len(recent),
                                     "promotions_in_window": len(promos), "ok": a_ok,
                                     "rule": "0 promotions in the last window"},
        "b_param_drift": {"median_grid_steps": drift_med, "refits_considered": min(len(refits), k_refits),
                          "rows": drift_rows[-12:], "ok": b_ok,
                          "rule": "median drift <= 1 grid step of the walk-forward"},
        "c_holdout_consistency": {"last": recent_inside, "m": m_holdouts, "ok": c_ok,
                                  "series": hold[-6:],
                                  "rule": f"held-out net inside the previous CI for {m_holdouts} refits running"},
        "time_decay_half_life_months": half_life_months,
        "weighting_in_force": ("time decay only (stable)" if stable else
                               "version discount (lambda = shared parameter share) x time"),
        "why": ("All three hold: the rule is settled, every version's trades count in full "
                "and only age discounts them." if stable else
                "At least one does not hold yet: trades from older versions are discounted "
                "by how different that version was, and a removed version's trades count zero."),
    }


# ──────────────────────────── the ledger view ────────────────────────────────

def _welch(a: list[float], b: list[float]) -> dict:
    a, b = np.asarray(a, float), np.asarray(b, float)
    if a.size < 2 or b.size < 2:
        return {"t": None, "verdict": "not enough trades"}
    va, vb = a.var(ddof=1) / a.size, b.var(ddof=1) / b.size
    se = math.sqrt(va + vb)
    t = float((a.mean() - b.mean()) / se) if se else 0.0
    return {"t": t, "diff_pct": float(a.mean() - b.mean()),
            "verdict": ("better" if t >= 2 else "worse" if t <= -2 else "not distinguishable")}


def ledger(strategy: str) -> dict:
    """Every version this strategy has traded under, head to head."""
    ensure_schema()
    chain = _version_chain(strategy)
    cur = current(strategy)
    out = []
    prev_pcts: list[float] | None = None
    for v in chain:
        rows = db.query(
            "SELECT net_pnl_usd, qty, entry_px FROM trades WHERE strategy=? AND params_hash=? "
            "AND qty*entry_px > 0", (strategy, v["params_hash"]))
        pcts = [100.0 * r["net_pnl_usd"] / (r["qty"] * r["entry_px"]) for r in rows]
        n = len(pcts)
        mean = float(np.mean(pcts)) if n else None
        se = float(np.std(pcts, ddof=1) / math.sqrt(n)) if n > 1 else None
        out.append({
            "version": v["version"], "params_hash": v["params_hash"],
            "is_current": v["params_hash"].rstrip("*") == cur["params_hash"],
            "backfilled": v["params_hash"].endswith("*"),
            "n": n, "mean_net_pct": mean, "se_pct": se,
            "win_rate": (100.0 * sum(1 for p in pcts if p > 0) / n) if n else None,
            "total_usd": float(sum(r["net_pnl_usd"] for r in rows)),
            "first_ts": v["first_ts"], "last_ts": v["last_ts"],
            "similarity_to_current": similarity(v.get("params"), cur["params"]) if v.get("params") else None,
            "vs_previous": _welch(pcts, prev_pcts) if prev_pcts is not None else None,
            "params_changed": (sorted(k for k in (v.get("params") or {})
                                      if k not in _NOT_RULE and (cur["params"].get(k) != v["params"][k]))
                               if v.get("params") else None),
        })
        prev_pcts = pcts
    return {"strategy": strategy, "current": {"version": cur["version"], "params_hash": cur["params_hash"]},
            "versions": out, "stability": stability(strategy, chain=chain),
            "note": ("A version is a distinct (class version, parameter hash). Trades before "
                     "tagging began carry the hash of the rule as it stands today, marked *. "
                     "vs_previous is Welch's t on per-trade net %; under 2 it is not "
                     "distinguishable, which is the honest answer at these sample sizes.")}


def all_ledgers() -> dict:
    from app.strategy.registry import studied
    return {"strategies": [ledger(s) for s in studied()], "generated_at": time.time()}
