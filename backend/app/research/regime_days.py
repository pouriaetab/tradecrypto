"""The day's weather, named from the data, and what each strategy earns in it.

    "going back to each day cycle characteristics for example bear, bull, slow
     trending up/down ... automatically the ai bot turn on and focus on the
     specific characteristic if recognized ... on overall market sentiment and
     then focused narrowly on the individual crypto"

TWO LAYERS, THIS MODULE IS THE FIRST
------------------------------------
Market level. Every trading day (America/Chicago) is read at READ_HOUR local
-- early enough to act on, late enough to have a shape -- and described by six
numbers that use only bars at or before that hour:

    breadth       share of tracked coins above their own 5-day average
    median_24h    the median coin's return over the last 24 hours
    btc_24h       the leader's return over the last 24 hours
    dispersion    cross-sectional spread of the coins' 24-hour returns
    vol_24h       BTC's realised volatility over the last 24 hours
    day_so_far    the median coin's move from local midnight to READ_HOUR

Those six are standardised and clustered with a Gaussian mixture (diagonal
covariance, EM, written out so it is inspectable), the number of clusters
chosen by BIC between 2 and 5 -- so the day types are found, not decreed. Each
cluster is then NAMED from its own centroid (bull/bear/flat by the median
return, broad/narrow by breadth, wild/quiet by volatility), so "bull-broad" is
a description of where the centroid sits, not an opinion.

Then the part that makes it usable: every active strategy is replayed over the
same history (research/backtest.py, its own exit, Robinhood's spread both
sides) and every entry is filed under the regime of its day. The scorecard
says, per (strategy, regime): n, net per trade, standard error, and how far
that cell sits from the strategy's overall mean in standard errors. A cell
that is not two standard errors from the overall is "indistinguishable" and
the router (below) leaves the size alone.

THE ROUTER -- a multiplier, not a switch
----------------------------------------
`multiplier(strategy)` reads today's regime (recorded once READ_HOUR has
passed) and the latest scorecard, and returns a size multiplier:

    m = 1 + (2 P - 1) * n / (n + MIN)        P = Phi((cell - overall) / se_diff)

but ONLY once the cell is two standard errors from the strategy's overall --
under that it is indistinguishable and m is exactly 1.0, no behaviour change.
P is the probability that the strategy does better in this weather than it
does in general; the n/(n+MIN) term shrinks the answer toward 1 when the cell
is thin. Clipped to [0.5, 1.5] so no regime can
either switch a strategy off or double it; a strategy that should be OFF in a
regime will show it on the scorecard first, and that is a promotion decision,
not a multiplier.

The multiplier is recorded on every signal's features (`regime`,
`regime_mult`) so the exit lab and the retrainer can judge whether it earned
its place, the same way everything else is judged.

No look-ahead anywhere: the regime of a day uses bars up to READ_HOUR of that
day; the replay entries are filed by their entry day; the scorecard is
re-fitted daily by the job, never inside the tick.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import time
from zoneinfo import ZoneInfo

import numpy as np

from app.core import db

TZ = ZoneInfo("America/Chicago")
READ_HOUR = 9
FEATURES = ("breadth", "median_24h", "btc_24h", "dispersion", "vol_24h", "day_so_far")
MIN_CELL = 40                     # same bar as the exit lab's verdicts
CLIP = (0.5, 1.5)


# ───────────────────────────── features per day ─────────────────────────────

def day_features(panel, symbols_keep: set[str] | None = None) -> list[dict]:
    """One row per local day with all six features at READ_HOUR, plus the
    median coin's rest-of-day return (for the value-of-knowing check)."""
    close = panel.close
    local = [_dt.datetime.fromtimestamp(float(t), TZ) for t in panel.ts]
    dates = np.array([d.date().toordinal() for d in local])
    hours = np.array([d.hour for d in local])
    cols = [i for i, s in enumerate(panel.symbols) if (symbols_keep is None or s in symbols_keep)]
    btc = panel.symbols.index("BTC") if "BTC" in panel.symbols else None
    out = []
    all_days = np.unique(dates)
    for day in all_days:
        idx = np.where(dates == day)[0]
        at = idx[hours[idx] == READ_HOUR]
        # A day needs its READ_HOUR bar. A FULL day is needed for the label
        # (rest-of-day); the last day in the panel may be today, still in
        # progress -- its features are readable the moment 09:00 has printed,
        # and that is exactly when the router needs them.
        partial = idx.size < 20
        if at.size == 0 or (partial and day != all_days[-1]):
            continue
        i = int(at[0])
        if i < 121:
            continue
        c_now = close[i, cols]
        c_24 = close[i - 24, cols]
        c_open = close[idx[0], cols]
        ma5 = np.nanmean(close[i - 120:i + 1][:, cols], axis=0)
        ok = np.isfinite(c_now) & np.isfinite(c_24) & (c_24 > 0)
        if ok.sum() < 5:
            continue
        r24 = (c_now[ok] / c_24[ok] - 1.0) * 100.0
        breadth = float(np.mean(c_now[ok] > ma5[ok]))
        okd = ok & np.isfinite(c_open) & (c_open > 0)
        dsf = float(np.median((c_now[okd] / c_open[okd] - 1.0) * 100.0)) if okd.any() else 0.0
        if btc is not None and np.isfinite(close[i, btc]) and np.isfinite(close[i - 24, btc]):
            btc24 = float((close[i, btc] / close[i - 24, btc] - 1.0) * 100.0)
            lr = np.diff(np.log(close[i - 24:i + 1, btc]))
            vol = float(np.nanstd(lr) * 100.0 * math.sqrt(24))
        else:
            btc24, vol = float(np.median(r24)), float(np.nanstd(r24))
        last = close[idx[-1], cols]
        okr = ok & np.isfinite(last)
        rest = (float(np.median((last[okr] / c_now[okr] - 1.0) * 100.0))
                if okr.any() and not partial else None)
        out.append({"date": _dt.date.fromordinal(int(day)).isoformat(),
                    "breadth": breadth, "median_24h": float(np.median(r24)), "btc_24h": btc24,
                    "dispersion": float(np.std(r24)), "vol_24h": vol, "day_so_far": dsf,
                    "rest_of_day_median_pct": rest})
    return out


# ───────────────────────── the mixture, written out ─────────────────────────

def _gmm(X: np.ndarray, k: int, iters: int = 200, seed: int = 0):
    rng = np.random.default_rng(seed)
    n, d = X.shape
    mu = X[rng.choice(n, k, replace=False)]
    var = np.ones((k, d))
    pi = np.full(k, 1.0 / k)
    ll_old = -np.inf
    for _ in range(iters):
        # E
        logp = np.zeros((n, k))
        for j in range(k):
            logp[:, j] = (np.log(pi[j]) - 0.5 * np.sum(np.log(2 * np.pi * var[j]))
                          - 0.5 * np.sum((X - mu[j]) ** 2 / var[j], axis=1))
        mx = logp.max(axis=1, keepdims=True)
        resp = np.exp(logp - mx)
        tot = resp.sum(axis=1, keepdims=True)
        resp /= tot
        ll = float((mx.squeeze() + np.log(tot.squeeze())).sum())
        # M
        nk = resp.sum(axis=0) + 1e-9
        pi = nk / n
        mu = (resp.T @ X) / nk[:, None]
        for j in range(k):
            var[j] = np.maximum((resp[:, j:j + 1] * (X - mu[j]) ** 2).sum(axis=0) / nk[j], 1e-4)
        if abs(ll - ll_old) < 1e-6:
            break
        ll_old = ll
    n_params = k * (2 * d) + (k - 1)
    bic = -2 * ll + n_params * math.log(n)
    return {"k": k, "mu": mu, "var": var, "pi": pi, "ll": ll, "bic": bic, "resp": resp}


def _name(centroid_z: np.ndarray, raw_mu: np.ndarray) -> str:
    z = dict(zip(FEATURES, centroid_z))
    tone = "bull" if z["median_24h"] > 0.3 else "bear" if z["median_24h"] < -0.3 else "flat"
    width = "broad" if z["breadth"] > 0.3 else "narrow" if z["breadth"] < -0.3 else ""
    heat = "wild" if z["vol_24h"] > 0.5 else "quiet" if z["vol_24h"] < -0.5 else ""
    turn = ""
    if tone == "bull" and z["day_so_far"] < -0.3:
        turn = "fading"
    elif tone == "bear" and z["day_so_far"] > 0.3:
        turn = "bouncing"
    return "-".join(x for x in (tone, width, heat, turn) if x)


def fit_regimes(rows: list[dict], k_range=(2, 3, 4, 5), seed: int = 0) -> dict:
    X = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    ok = np.all(np.isfinite(X), axis=1)
    X, rows = X[ok], [r for r, o in zip(rows, ok) if o]
    mu0, sd0 = X.mean(axis=0), X.std(axis=0) + 1e-9
    Z = (X - mu0) / sd0
    fits = []
    for k in k_range:
        if len(Z) < 10 * k:
            continue
        best = min((_gmm(Z, k, seed=seed + s) for s in range(3)), key=lambda f: -f["ll"])
        fits.append(best)
    if not fits:
        return {"available": False, "why": f"only {len(Z)} days"}
    best = min(fits, key=lambda f: f["bic"])
    labels_idx = best["resp"].argmax(axis=1)
    names = [_name(best["mu"][j], best["mu"][j] * sd0 + mu0) for j in range(best["k"])]
    # two clusters can earn the same name; keep them distinct
    seen: dict[str, int] = {}
    for j, nm in enumerate(names):
        seen[nm] = seen.get(nm, 0) + 1
        if seen[nm] > 1:
            names[j] = f"{nm}-{seen[nm]}"
    labels = {r["date"]: names[int(j)] for r, j in zip(rows, labels_idx)}
    centroids = [{"name": names[j], "share": float(best["pi"][j]),
                  **{f: float(v) for f, v in zip(FEATURES, best["mu"][j] * sd0 + mu0)}}
                 for j in range(best["k"])]
    return {"available": True, "k": best["k"], "bic_by_k": {f["k"]: f["bic"] for f in fits},
            "names": names, "centroids": centroids, "labels": labels,
            "standardise": {"mu": mu0.tolist(), "sd": sd0.tolist()},
            "model": {"mu": best["mu"].tolist(), "var": best["var"].tolist(), "pi": best["pi"].tolist()}}


def classify(model: dict, row: dict) -> tuple[str, float]:
    """Regime of one day-row under a fitted model, with its responsibility."""
    x = np.array([row[f] for f in FEATURES], dtype=float)
    z = (x - np.array(model["standardise"]["mu"])) / np.array(model["standardise"]["sd"])
    mu, var, pi = (np.array(model["model"]["mu"]), np.array(model["model"]["var"]),
                   np.array(model["model"]["pi"]))
    logp = np.array([np.log(pi[j]) - 0.5 * np.sum(np.log(2 * np.pi * var[j]))
                     - 0.5 * np.sum((z - mu[j]) ** 2 / var[j]) for j in range(len(pi))])
    p = np.exp(logp - logp.max()); p /= p.sum()
    j = int(p.argmax())
    return model["names"][j], float(p[j])


# ───────────────────────── per-regime strategy scorecard ────────────────────

def _stats(v: list[float]) -> dict:
    a = np.asarray(v, dtype=float)
    a = a[np.isfinite(a)]
    n = int(a.size)
    if not n:
        return {"n": 0}
    return {"n": n, "mean_net_pct": float(a.mean()),
            "se_pct": float(a.std(ddof=1) / math.sqrt(n)) if n > 1 else 0.0,
            "win_rate": float((a > 0).mean() * 100.0)}


def scorecard(panel, labels: dict[str, str], cost_bps_per_side: float) -> dict:
    from app.research import backtest as bt
    from app.strategy.registry import studied, build
    from app.core import scheduler as _sched

    out: dict[str, dict] = {}
    local_dates = [_dt.datetime.fromtimestamp(float(t), TZ).date().isoformat() for t in panel.ts]
    for name in studied():
        try:
            strat = build(name)
            p = _sched._research_panel_for(name, panel) if hasattr(_sched, "_research_panel_for") else panel
            dates = (local_dates if p is panel else
                     [_dt.datetime.fromtimestamp(float(t), TZ).date().isoformat() for t in p.ts])
            res = bt.run_backtest(p, strat, cost_bps_per_side=cost_bps_per_side,
                                  apply_hurdle=False, max_concurrent=10_000)
        except Exception as exc:
            out[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            continue
        by: dict[str, list] = {}
        allv = []
        for tr in res.trades:
            reg = labels.get(dates[min(tr.entry_i, len(dates) - 1)])
            if reg is None:
                continue
            v = float(tr.net_ret * 100.0)
            by.setdefault(reg, []).append(v)
            allv.append(v)
        overall = _stats(allv)
        cells = {}
        for reg, v in by.items():
            c = _stats(v)
            if c["n"] and overall.get("n"):
                sed = math.hypot(c["se_pct"], overall["se_pct"])
                c["vs_overall_pct"] = c["mean_net_pct"] - overall["mean_net_pct"]
                c["sigmas"] = (c["vs_overall_pct"] / sed) if sed else 0.0
                c["verdict"] = ("better in this weather" if c["sigmas"] >= 2 else
                                "worse in this weather" if c["sigmas"] <= -2 else
                                "indistinguishable from its overall")
            cells[reg] = c
        out[name] = {"overall": overall, "by_regime": cells}
    return out


# ───────────────────────────── the daily study ──────────────────────────────

def study(days: int = 4 * 365, panel=None) -> dict:
    from app.execution import engine
    from app.data import selection
    t0 = time.time()
    if panel is None:
        panel = engine.build_panel(refresh=False, granularity=3600, limit=days * 24 + 200)
    if panel.T < 24 * 60:
        return {"available": False, "why": f"only {panel.T} hourly bars"}
    keep = set(selection.tradeable_symbols()) | set(getattr(selection, "CORE", []))
    rows = day_features(panel, keep)
    fit = fit_regimes(rows)
    if not fit.get("available"):
        return fit
    try:
        from app.execution import rh_spread
        side = float(rh_spread.DEFAULT_SPREAD_PCT) * 100.0
    except Exception:
        side = 95.0
    card = scorecard(panel, fit["labels"], side)
    # value of knowing: the median coin's rest-of-day by regime
    rest: dict[str, list] = {}
    for r in rows:
        lab = fit["labels"].get(r["date"])
        if lab and r.get("rest_of_day_median_pct") is not None:
            rest.setdefault(lab, []).append(r["rest_of_day_median_pct"])
    rest_by = {k: _stats(v) for k, v in rest.items()}
    counts = {}
    for lab in fit["labels"].values():
        counts[lab] = counts.get(lab, 0) + 1
    # today's regime, if READ_HOUR has passed, from the same model
    today_row = rows[-1] if rows else None
    today = None
    if today_row:
        today_date = _dt.datetime.now(TZ).date().isoformat()
        if today_row["date"] == today_date:
            name, p = classify(fit, today_row)
            today = {"date": today_date, "regime": name, "p": p,
                     **{f: today_row[f] for f in FEATURES}}
    return {"available": True, "read_hour": READ_HOUR, "days": len(rows), "coins": len(keep),
            "k": fit["k"], "bic_by_k": fit["bic_by_k"], "centroids": fit["centroids"],
            "counts": counts, "rest_of_day_by_regime": rest_by,
            "scorecard": card, "today": today, "labels_recent": dict(list(fit["labels"].items())[-30:]),
            "model": {"names": fit["names"], "standardise": fit["standardise"], "model": fit["model"]},
            "cost_bps_per_side": side, "took_s": time.time() - t0,
            "definition": (f"Each day read at {READ_HOUR:02d}:00 Austin from the six features; "
                           f"Gaussian mixture, k by BIC; names from the centroid. Scorecard = every "
                           f"active strategy replayed with its own exit, {side:.0f} bps each side, "
                           f"entries filed by the regime of their day.")}


def record(res: dict) -> None:
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "regime_days", f"k={res.get('k')}", json.dumps({"read_hour": READ_HOUR}),
                json.dumps(res, default=str),
                (f"today: {res['today']['regime']}" if res.get("today") else "today not yet readable")))
    if res.get("today"):
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES ('regime_today', ?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                   (json.dumps(res["today"]), time.time()))


def latest() -> dict | None:
    row = db.query_one("SELECT ts, result_json FROM runs WHERE kind='regime_days' ORDER BY ts DESC LIMIT 1")
    if not row:
        return None
    d = json.loads(row["result_json"]); d["ran_at"] = row["ts"]
    return d


def today() -> dict | None:
    try:
        row = db.query_one("SELECT value, updated_ts FROM app_state WHERE key='regime_today'")
    except Exception:
        return None
    if not row:
        return None
    d = json.loads(row["value"])
    if d.get("date") != _dt.datetime.now(TZ).date().isoformat():
        return None                       # yesterday's weather is not today's
    return d


# ─────────────────────────────── the router ─────────────────────────────────

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def multiplier(strategy: str) -> dict:
    """Size multiplier for this strategy in today's regime. 1.0 whenever the
    data cannot tell today's weather apart for this strategy."""
    base = {"multiplier": 1.0, "regime": None, "why": "no regime reading for today yet"}
    t = today()
    if not t:
        return base
    res = latest()
    if not res or not res.get("available"):
        return {**base, "regime": t["regime"], "why": "no scorecard yet"}
    card = (res.get("scorecard") or {}).get(strategy) or {}
    cell = (card.get("by_regime") or {}).get(t["regime"])
    overall = card.get("overall") or {}
    if not cell or not cell.get("n") or not overall.get("n"):
        return {**base, "regime": t["regime"],
                "why": f"no replayed entries for {strategy} in '{t['regime']}' days"}
    sed = math.hypot(cell.get("se_pct", 0.0), overall.get("se_pct", 0.0))
    z = ((cell["mean_net_pct"] - overall["mean_net_pct"]) / sed) if sed else 0.0
    if abs(z) < 2.0:
        # The same bar as every other decision here: under two standard
        # errors the cell is indistinguishable from the strategy's overall,
        # and an indistinguishable cell changes nothing. Without this line a
        # 0.25σ wobble tilted size by 11%, which is trading the noise.
        return {"multiplier": 1.0, "regime": t["regime"], "n_cell": cell["n"],
                "cell_net_pct": cell["mean_net_pct"], "overall_net_pct": overall["mean_net_pct"],
                "sigmas": z, "p_better_than_usual": _phi(z),
                "why": (f"'{t['regime']}' days: {strategy} {cell['mean_net_pct']:+.2f}%/trade over "
                        f"{cell['n']} replayed entries vs {overall['mean_net_pct']:+.2f}% overall "
                        f"({z:+.1f}σ) — indistinguishable, size unchanged")}
    p = _phi(z)
    shrink = cell["n"] / (cell["n"] + MIN_CELL)
    m = 1.0 + (2.0 * p - 1.0) * shrink
    m = max(CLIP[0], min(CLIP[1], m))
    return {"multiplier": m, "regime": t["regime"], "p_better_than_usual": p,
            "n_cell": cell["n"], "cell_net_pct": cell["mean_net_pct"],
            "overall_net_pct": overall["mean_net_pct"], "sigmas": cell.get("sigmas"),
            "why": (f"'{t['regime']}' days: {strategy} netted {cell['mean_net_pct']:+.2f}%/trade over "
                    f"{cell['n']} replayed entries vs {overall['mean_net_pct']:+.2f}% overall "
                    f"({cell.get('sigmas', 0):+.1f}σ); P(better) {p:.2f}, shrunk by n/(n+{MIN_CELL}) -> x{m:.2f}")}
