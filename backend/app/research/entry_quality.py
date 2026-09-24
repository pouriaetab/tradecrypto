"""Entry quality: at the moment a strategy fires, what is the probability the
trade wins after costs -- fitted on the strategy's own historical entries,
validated on the last quarter of them, and reported as what it is.

WHY THIS AND NOT A LATENESS GATE
--------------------------------
day_climb's whole loss lives in one bucket: climbs that fail and are held to
the 24-hour exit (8 of 39 live trades, -5.45% each; the other 31 +1.5%).
Lateness -- how much of the move happened in the last hour, how close to the
two-hour high -- does not predict which climbs fail (research/entry_lateness,
13,178 entries, rho ~ 0). So the question is the general one: of everything
knowable at the signal bar, what separates the entries that pay from the ones
that do not?

WHAT IT FITS
------------
An L2-penalised logistic regression (the same IRLS fit `research/breakout.py`
uses for the false-breakout veto -- written out, every coefficient and its
standard error visible) on:

    the strategy's own signal features     (climb_pct, coin_vol_pct, target_pct,
                                            volume_vs_normal, trend_48h_pct ...
                                            whatever the strategy records)
    lateness                               last_hour_pct, vs_high_2h_pct
    the coin's context                     ret_24h_pct, day_so_far_pct
    the market's context                   breadth, median_24h, btc_24h,
                                            dispersion, hour of day (sin/cos)
    the day's regime                       one column per regime found by
                                            research/regime_days, if fitted

Label: the trade's net return after Robinhood's spread both sides was
positive, under the strategy's own exit, from the backtester.

HOW IT IS JUDGED
----------------
Chronological split: the first three quarters of entries fit, the last
quarter is read once. Reported on the held-out quarter:

    AUC with a bootstrap 95% interval      does it rank at all?
    net per trade by predicted quintile    does the ranking find MONEY?
    top quintile minus bottom quintile     in standard errors
    calibration table                      does 0.6 mean 60%?

Verdict "usable for ranking" needs the AUC interval above 0.5 AND the top
quintile ahead of the bottom by two standard errors on the held-out quarter.
Anything less is "not yet", and nothing here changes a trade: this module
writes a report. Promotion into the ranking is a separate, deliberate step
through the same champion-versus-challenger machinery as everything else.
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
HOLDOUT_FRAC = 0.25
MIN_EVENTS = 300
_SKIP = {"calibration_n", "keeps_after_costs_pct", "breakeven_round_trip_pct", "hour_weight",
         "entry_hour_local", "volume_prev", "climb_bars", "burst_n", "burst_rank", "regime",
         "regime_mult"}


def _market_context(panel, t: int, cols: list[int], btc: int | None) -> dict:
    close = panel.close
    c_now, c_24 = close[t, cols], close[t - 24, cols]
    ok = np.isfinite(c_now) & np.isfinite(c_24) & (c_24 > 0)
    r24 = (c_now[ok] / c_24[ok] - 1.0) * 100.0 if ok.any() else np.array([0.0])
    ma5 = np.nanmean(close[max(0, t - 120):t + 1][:, cols], axis=0)
    breadth = float(np.mean(c_now[ok] > ma5[ok])) if ok.any() else 0.5
    if btc is not None and np.isfinite(close[t, btc]) and np.isfinite(close[t - 24, btc]) and close[t - 24, btc] > 0:
        btc24 = float((close[t, btc] / close[t - 24, btc] - 1.0) * 100.0)
    else:
        btc24 = float(np.median(r24))
    return {"breadth": breadth, "median_24h": float(np.median(r24)), "btc_24h": btc24,
            "dispersion": float(np.std(r24))}


def build_events(strategy: str, panel=None, days: int = 4 * 365,
                 cost_bps_per_side: float | None = None) -> dict:
    """Every historical entry with its features and its outcome."""
    from app.execution import engine
    from app.research import backtest as bt
    from app.strategy.registry import build
    from app.data import selection

    strat = build(strategy)
    bs = int(getattr(strat, "bar_seconds", 3600) or 3600)
    if panel is None:
        panel = engine.build_panel(refresh=False, granularity=bs, limit=days * (86400 // bs) + 400)
    if cost_bps_per_side is None:
        try:
            from app.execution import rh_spread
            cost_bps_per_side = float(rh_spread.DEFAULT_SPREAD_PCT) * 100.0
        except Exception:
            cost_bps_per_side = 95.0
    res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps_per_side,
                          apply_hurdle=False, max_concurrent=10_000)
    bph = max(1, int(round(3600.0 / bs)))
    keep = set(selection.tradeable_symbols()) | set(getattr(selection, "CORE", []))
    cols = [i for i, s in enumerate(panel.symbols) if s in keep] or list(range(panel.N))
    btc = panel.symbols.index("BTC") if "BTC" in panel.symbols else None
    sym_idx = {s: i for i, s in enumerate(panel.symbols)}
    close, high = panel.close, panel.high
    local = [_dt.datetime.fromtimestamp(float(ts), TZ) for ts in panel.ts]
    labels = {}
    try:
        from app.research import regime_days
        rd = regime_days.latest()
        if rd and rd.get("available"):
            # full label set is not stored; rebuild from the stored model
            model = {"names": rd["model"]["names"], "standardise": rd["model"]["standardise"],
                     "model": rd["model"]["model"]}
            rows = regime_days.day_features(panel, keep)
            for r in rows:
                labels[r["date"]] = regime_days.classify(model, r)[0]
    except Exception:
        labels = {}
    regimes = sorted(set(labels.values()))

    events = []
    sig_cache: dict[int, dict] = {}
    for tr in res.trades:
        t = tr.entry_i - 1
        k = sym_idx[tr.symbol]
        if t < 26 * bph + 2:
            continue
        if t not in sig_cache:
            try:
                sig_cache[t] = {s.symbol: s for s in strat.generate(panel, t)}
            except Exception:
                sig_cache[t] = {}
        sig = sig_cache[t].get(tr.symbol)
        if sig is None:
            continue
        feats = {k_: float(v) for k_, v in (sig.features or {}).items()
                 if k_ not in _SKIP and isinstance(v, (int, float)) and np.isfinite(float(v))}
        px = close[t, k]
        prev_h = close[t - bph, k]
        hi2 = np.nanmax(high[t - 2 * bph:t, k]) if high is not None else np.nan
        c24 = close[t - 24 * bph, k]
        day_start = t
        while day_start > 0 and local[day_start - 1].date() == local[t].date():
            day_start -= 1
        c_open = close[day_start, k]
        if not (np.isfinite(px) and np.isfinite(prev_h) and prev_h > 0 and np.isfinite(hi2) and hi2 > 0
                and np.isfinite(c24) and c24 > 0 and np.isfinite(c_open) and c_open > 0):
            continue
        hr = local[t].hour
        row = {**feats,
               "last_hour_pct": (px / prev_h - 1.0) * 100.0,
               "vs_high_2h_pct": (px / hi2 - 1.0) * 100.0,
               "ret_24h_pct": (px / c24 - 1.0) * 100.0,
               "day_so_far_pct": (px / c_open - 1.0) * 100.0,
               "hour_sin": math.sin(2 * math.pi * hr / 24.0),
               "hour_cos": math.cos(2 * math.pi * hr / 24.0),
               **_market_context(panel, t, cols, btc)}
        lab = labels.get(local[t].date().isoformat())
        for rg in regimes:
            row[f"regime={rg}"] = 1.0 if lab == rg else 0.0
        events.append({"symbol": tr.symbol, "t": int(t), "ts": float(panel.ts[t]),
                       "features": row, "net_pct": float(tr.net_ret * 100.0),
                       "won": 1.0 if tr.net_ret > 0 else 0.0})
    events.sort(key=lambda e: e["ts"])
    return {"strategy": strategy, "events": events, "cost_bps_per_side": cost_bps_per_side,
            "regimes": regimes, "span_days": float((panel.ts[-1] - panel.ts[0]) / 86400.0)}


def _quintiles(p: np.ndarray, net: np.ndarray) -> list[dict]:
    order = np.argsort(p)
    out = []
    for q in range(5):
        idx = order[q * len(order) // 5:(q + 1) * len(order) // 5]
        if idx.size == 0:
            continue
        v = net[idx]
        out.append({"quintile": q + 1, "n": int(idx.size), "p_range": [float(p[idx].min()), float(p[idx].max())],
                    "mean_net_pct": float(v.mean()),
                    "se_pct": float(v.std(ddof=1) / math.sqrt(idx.size)) if idx.size > 1 else 0.0,
                    "win_rate": float((v > 0).mean() * 100.0)})
    return out


def fit(strategy: str, panel=None, days: int = 4 * 365) -> dict:
    from app.research import breakout as bo
    from app.research import stats as st

    t0 = time.time()
    built = build_events(strategy, panel=panel, days=days)
    ev = built["events"]
    if len(ev) < MIN_EVENTS:
        return {"available": False, "strategy": strategy, "n_events": len(ev),
                "why": f"{len(ev)} historical entries; need {MIN_EVENTS} before a fit means anything"}
    names = sorted({k for e in ev for k in e["features"]})
    X = np.array([[e["features"].get(k, 0.0) for k in names] for e in ev], dtype=float)
    y = np.array([e["won"] for e in ev], dtype=float)
    net = np.array([e["net_pct"] for e in ev], dtype=float)
    # drop constant columns (a regime that never occurs in this span, say)
    keep = [i for i in range(X.shape[1]) if np.nanstd(X[:, i]) > 0]
    names = [names[i] for i in keep]
    X = X[:, keep]
    split = int(len(ev) * (1.0 - HOLDOUT_FRAC))
    Xtr, ytr, Xho, yho, netho = X[:split], y[:split], X[split:], y[split:], net[split:]
    model = bo.fit_logistic(Xtr, ytr, names, l2=1.0)
    p_ho = model.predict(Xho)
    auc = bo.auc(yho, p_ho)
    rng = np.random.default_rng(0)
    aucs = []
    for _ in range(200):
        idx = rng.integers(0, len(yho), len(yho))
        if yho[idx].min() == yho[idx].max():
            continue
        aucs.append(bo.auc(yho[idx], p_ho[idx]))
    auc_ci = [float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))] if aucs else [None, None]
    quint = _quintiles(p_ho, netho)
    top, bot = (quint[-1], quint[0]) if len(quint) >= 2 else (None, None)
    spread = None
    if top and bot:
        sed = math.hypot(top["se_pct"], bot["se_pct"])
        spread = {"top_minus_bottom_pct": top["mean_net_pct"] - bot["mean_net_pct"],
                  "sigmas": ((top["mean_net_pct"] - bot["mean_net_pct"]) / sed) if sed else 0.0}
    # "trade only the better half": what the holdout would have netted
    med = float(np.median(p_ho))
    above = netho[p_ho >= med]
    half = {"n": int(above.size), "mean_net_pct": float(above.mean()) if above.size else None,
            "all_mean_net_pct": float(netho.mean())}
    usable = bool(auc_ci[0] is not None and auc_ci[0] > 0.5 and spread and spread["sigmas"] >= 2.0)
    out = {
        "available": True, "strategy": strategy, "n_events": len(ev), "n_train": int(split),
        "n_holdout": int(len(ev) - split), "span_days": built["span_days"],
        "features": names, "regimes_in_model": built["regimes"],
        "holdout": {"auc": float(auc), "auc_ci95": auc_ci, "brier": float(bo.brier(yho, p_ho)),
                    "base_rate": float(yho.mean()), "quintiles": quint, "top_vs_bottom": spread,
                    "better_half": half, "calibration": bo.calibration(yho, p_ho, bins=8)},
        "coefficients": model.coefficients()[:15],
        "verdict": ("usable for ranking — the held-out quarter separates winners from losers by "
                    "more than two standard errors" if usable else
                    "not yet — ranks a little but the held-out money difference is inside the noise"),
        "usable_for_ranking": usable,
        "cost_bps_per_side": built["cost_bps_per_side"], "took_s": time.time() - t0,
        "note": ("Chronological split; the last quarter of entries is read once. This report "
                 "changes no trade: promotion into the ranking is a separate step."),
    }
    return out


def record(res: dict) -> None:
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "entry_quality", res.get("strategy", "?"), json.dumps({"holdout_frac": HOLDOUT_FRAC}),
                json.dumps(res, default=str), (res.get("verdict") or res.get("why") or "")[:400]))


def latest(strategy: str | None = None) -> dict | None:
    q = "SELECT ts, label, result_json FROM runs WHERE kind='entry_quality'"
    args: tuple = ()
    if strategy:
        q += " AND label=?"; args = (strategy,)
    row = db.query_one(q + " ORDER BY ts DESC LIMIT 1", args)
    if not row:
        return None
    d = json.loads(row["result_json"]); d["ran_at"] = row["ts"]
    return d


def latest_all() -> dict:
    # studied(), not ACTIVE: a retired strategy kept in the lab is still fitted.
    from app.strategy.registry import studied
    return {"strategies": {s: latest(s) for s in studied()}}
