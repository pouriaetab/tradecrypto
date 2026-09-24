"""Each strategy, day by day: what it did, what changed in it, and how it
stands against the others -- the strategy-level view of the Daily tab.

    "how successful or not it was and how daily it was improved ... compare and
     distinguish these changed improvements per day for that strategy ... what
     change of direction was done ... how this strategy fares against all other
     ones on day to day and overall periods"

Everything here is read from tables the desk already writes: trades (the
per-strategy book -- invariant 12, never a combined curve), signals (the
funnel), model_versions (every refit, promoted or refused), strategy_state
(the posterior and allocation), events (pauses, promotions, exit-lab findings)
and the exit lab's counterfactuals. Nothing is estimated that is not counted.

The honest parts, stated once so the page does not have to:

  * Per-trade numbers carry a standard error. A day with two trades has no
    standard error worth printing, so day rows show n and the page compares
    DAYS only through the period summaries.
  * "Improved" is a comparison of today's per-trade net against the strategy's
    own earlier trades (Welch's t). Under two standard errors it is "not
    distinguishable", which will be the answer most days -- that is the
    sample size, not the page being coy.
  * Rank among peers is by dollars AND by per-trade percent, because a strategy
    that trades nine times a day will win the dollar column on volume alone.
  * The change log is what the desk RECORDED changing (refits, pauses,
    promotions, parameter overrides, pending ideas firing). A code edit that
    nobody logged is not in it, and that absence is itself the lesson.
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


def _day_bounds(day: str) -> tuple[float, float]:
    d = _dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    return d.timestamp(), (d + _dt.timedelta(days=1)).timestamp()


def _day_of(ts: float) -> str:
    return _dt.datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d")


def _days_between(start: str, end: str) -> list[str]:
    a = _dt.datetime.strptime(start, "%Y-%m-%d").date()
    b = _dt.datetime.strptime(end, "%Y-%m-%d").date()
    out, cur = [], a
    while cur <= b:
        out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def _pct(t: dict) -> float | None:
    basis = float(t.get("entry_px") or 0.0) * abs(float(t.get("qty") or 0.0))
    if basis <= 0 or t.get("net_pnl_usd") is None:
        return None
    return float(t["net_pnl_usd"]) / basis * 100.0


def _stats(pcts: list[float], usd: list[float]) -> dict:
    n = len(pcts)
    if not n:
        return {"n": 0, "net_usd": 0.0, "mean_net_pct": None, "se_pct": None,
                "win_rate": None, "best_pct": None, "worst_pct": None, "profit_factor": None}
    a = np.asarray(pcts, dtype=float)
    u = np.asarray(usd, dtype=float)
    gains = float(u[u > 0].sum()); losses = float(-u[u < 0].sum())
    return {
        "n": n, "net_usd": float(u.sum()), "gross_usd": None,
        "mean_net_pct": float(a.mean()),
        "se_pct": float(a.std(ddof=1) / math.sqrt(n)) if n > 1 else None,
        "win_rate": float((a > 0).mean() * 100.0),
        "best_pct": float(a.max()), "worst_pct": float(a.min()),
        "profit_factor": (gains / losses) if losses > 0 else (None if gains == 0 else float("inf")),
    }


def _welch(a: list[float], b: list[float]) -> dict:
    """Is set a's mean different from set b's? Welch's t, honest about n."""
    if len(a) < 2 or len(b) < 2:
        return {"t": None, "diff_pct": (float(np.mean(a)) - float(np.mean(b))) if a and b else None,
                "verdict": "not enough trades on one side to compare"}
    x, y = np.asarray(a, float), np.asarray(b, float)
    se = math.sqrt(x.var(ddof=1) / x.size + y.var(ddof=1) / y.size)
    diff = float(x.mean() - y.mean())
    t = diff / se if se > 0 else 0.0
    return {"t": t, "diff_pct": diff,
            "verdict": ("better than its earlier trades" if t >= 2 else
                        "worse than its earlier trades" if t <= -2 else
                        "not distinguishable from its earlier trades")}


def _beta_ci(wins: int, n: int, level: float = 0.9) -> tuple[float, float] | None:
    if n <= 0:
        return None
    try:
        from scipy import stats as sps
        a, b = 1 + wins, 1 + (n - wins)
        q = (1 - level) / 2
        return (float(sps.beta.ppf(q, a, b)) * 100.0, float(sps.beta.ppf(1 - q, a, b)) * 100.0)
    except Exception:
        return None


def _drawdown_usd(cum: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in cum:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst


def _change_log(strategy: str, t0: float, t1: float) -> list[dict]:
    """What the desk recorded changing about this strategy in the window."""
    out: list[dict] = []
    try:
        from app.research import retrain as _rt
        _rt.ensure_schema()               # a fresh database has no model_versions yet
    except Exception:
        pass
    for r in db.query(
            "SELECT fitted_ts, verdict, promoted, trigger, params_json, comparison "
            "FROM model_versions WHERE strategy=? AND fitted_ts >= ? AND fitted_ts < ?",
            (strategy, t0, t1)):
        out.append({"ts": float(r["fitted_ts"]), "kind": "retrain",
                    "text": ("PROMOTED: " if r["promoted"] else "refit, ") + (r["verdict"] or ""),
                    "detail": (r["trigger"] or ""), "promoted": bool(r["promoted"])})
    like = f"%{strategy}%"
    for r in db.query(
            "SELECT ts, category, message FROM events WHERE ts >= ? AND ts < ? "
            "AND category IN ('retrain','relearn','pending','exit_lab','feedback','engine','universe','report') "
            "AND message LIKE ? ORDER BY ts", (t0, t1, like)):
        msg = r["message"] or ""
        if r["category"] == "retrain" and "rejected" in msg and any(o["kind"] == "retrain" for o in out):
            continue          # already covered by the model_versions row
        out.append({"ts": float(r["ts"]), "kind": r["category"], "text": msg[:240]})
    # allocation / status transitions
    prev = db.query_one(
        "SELECT status, allocation_frac FROM strategy_state WHERE strategy=? AND ts < ? "
        "ORDER BY ts DESC LIMIT 1", (strategy, t0))
    rows = db.query(
        "SELECT ts, status, allocation_frac, reason FROM strategy_state "
        "WHERE strategy=? AND ts >= ? AND ts < ? ORDER BY ts", (strategy, t0, t1))
    last = (prev["status"], float(prev["allocation_frac"] or 0)) if prev else None
    for r in rows:
        cur = (r["status"], float(r["allocation_frac"] or 0))
        if last is not None and cur != last:
            out.append({"ts": float(r["ts"]), "kind": "allocation",
                        "text": f"{last[0]} {last[1]:.3f} -> {cur[0]} {cur[1]:.3f}: {(r['reason'] or '')[:160]}"})
        last = cur
    out.sort(key=lambda x: x["ts"])
    return out


def _funnel(strategy: str, t0: float, t1: float) -> dict:
    r = db.query_one(
        "SELECT COUNT(*) n, SUM(CASE WHEN decision LIKE 'taken%' THEN 1 ELSE 0 END) taken, "
        "COUNT(DISTINCT symbol) coins FROM signals WHERE strategy=? AND ts >= ? AND ts < ?",
        (strategy, t0, t1))
    reasons = db.query(
        "SELECT reject_reason, COUNT(*) n FROM signals WHERE strategy=? AND ts >= ? AND ts < ? "
        "AND decision='rejected' GROUP BY reject_reason ORDER BY n DESC LIMIT 3",
        (strategy, t0, t1))
    return {"signals": int(r["n"] or 0), "taken": int(r["taken"] or 0), "coins": int(r["coins"] or 0),
            "top_reject_reasons": [{"reason": (x["reject_reason"] or "")[:140], "n": x["n"]} for x in reasons]}


def _exit_lab_for(strategy: str) -> dict | None:
    try:
        from app.research import exit_lab
        st = exit_lab.standings(strategy=strategy)
    except Exception:
        return None
    rules = st.get("rules") or []
    live = next((r for r in rules if r.get("is_live_rule")), None)
    best = next((r for r in rules if not r.get("is_live_rule")), None)
    if not rules:
        return None
    return {"n_trades": st.get("n_trades"), "as_traded_pct": live["mean_net_pct"] if live else None,
            "best_rule": best["rule"] if best else None,
            "best_rule_pct": best["mean_net_pct"] if best else None,
            "best_rule_verdict": best.get("verdict") if best else None,
            "giveback_pts": (st.get("giveback") or {}).get("given_back_pts")}


def _posterior(strategy: str) -> dict | None:
    r = db.query_one("SELECT ts, status, allocation_frac, posterior_json, reason FROM strategy_state "
                     "WHERE strategy=? ORDER BY ts DESC LIMIT 1", (strategy,))
    if not r:
        return None
    try:
        p = json.loads(r["posterior_json"] or "{}")
    except Exception:
        p = {}
    return {"ts": float(r["ts"]), "status": r["status"], "allocation_frac": float(r["allocation_frac"] or 0),
            "p_edge_above_hurdle": p.get("prob_edge_above_hurdle"),
            "hit_rate_ci90": (p.get("hit_rate_posterior") or {}).get("ci90"),
            "n_trades_considered": p.get("n_trades_considered"), "reason": (r["reason"] or "")[:160]}


def report(start: str, end: str, mode: str = "paper") -> dict:
    """The strategy-level view for a range of local days (inclusive)."""
    days = _days_between(start, end)
    t0, _ = _day_bounds(days[0])
    _, t1 = _day_bounds(days[-1])
    trades = db.query(
        "SELECT id, strategy, symbol, qty, entry_px, exit_px, ts_open, ts_close, holding_s, "
        "gross_pnl_usd, cost_usd, net_pnl_usd FROM trades WHERE mode=? AND ts_close >= ? AND ts_close < ? "
        "ORDER BY ts_close", (mode, t0, t1))
    earlier = db.query(
        "SELECT strategy, qty, entry_px, net_pnl_usd FROM trades WHERE mode=? AND ts_close < ?", (mode, t0))
    # active roster, so a strategy that fired but never traded still appears
    try:
        from app.strategy.registry import studied
        roster = list(studied())
    except Exception:
        roster = []
    names = sorted(set(roster) | {t["strategy"] for t in trades}
                   | {r["strategy"] for r in db.query(
                       "SELECT DISTINCT strategy FROM signals WHERE ts >= ? AND ts < ?", (t0, t1))})

    by_day: dict[str, dict[str, list]] = {s: {d: [] for d in days} for s in names}
    for t in trades:
        d = _day_of(t["ts_close"])
        if d in by_day.get(t["strategy"], {}):
            by_day[t["strategy"]][d].append(dict(t))
    hist: dict[str, list[float]] = {}
    for r in earlier:
        p = _pct(dict(r))
        if p is not None:
            hist.setdefault(r["strategy"], []).append(p)

    strategies: dict[str, dict] = {}
    for s in names:
        rows = []
        cum = 0.0
        cum_series = []
        seen_pcts: list[float] = list(hist.get(s, []))
        all_pcts: list[float] = []
        all_usd: list[float] = []
        for d in days:
            d0, d1 = _day_bounds(d)
            ts_ = by_day[s][d]
            pcts = [p for p in (_pct(t) for t in ts_) if p is not None]
            usd = [float(t["net_pnl_usd"] or 0.0) for t in ts_]
            st = _stats(pcts, usd)
            st["gross_usd"] = float(sum(float(t["gross_pnl_usd"] or 0.0) for t in ts_))
            st["cost_usd"] = float(sum(float(t["cost_usd"] or 0.0) for t in ts_))
            st["median_hold_h"] = (float(np.median([float(t["holding_s"] or 0) for t in ts_])) / 3600.0
                                   if ts_ else None)
            cum += st["net_usd"]
            cum_series.append(cum)
            vs = _welch(pcts, seen_pcts) if pcts else {"t": None, "diff_pct": None,
                                                        "verdict": "no trades this day"}
            rows.append({
                "day": d, **st, "cum_net_usd": cum,
                "trades": [{"symbol": t["symbol"], "net_usd": float(t["net_pnl_usd"] or 0),
                            "net_pct": _pct(t), "hold_h": float(t["holding_s"] or 0) / 3600.0,
                            "ts_open": float(t["ts_open"]), "ts_close": float(t["ts_close"])} for t in ts_],
                "funnel": _funnel(s, d0, d1),
                "vs_earlier": vs,
                "changes": _change_log(s, d0, d1),
            })
            seen_pcts.extend(pcts)
            all_pcts.extend(pcts)
            all_usd.extend(usd)
        overall = _stats(all_pcts, all_usd)
        traded_days = [r for r in rows if r["n"]]
        wins = sum(1 for p in all_pcts if p > 0)
        overall.update({
            "days_traded": len(traded_days),
            "days_positive": sum(1 for r in traded_days if r["net_usd"] > 0),
            "max_drawdown_usd": _drawdown_usd(cum_series),
            "hit_rate_ci90": _beta_ci(wins, len(all_pcts)),
            "cost_usd": float(sum(r["cost_usd"] for r in rows)),
            "gross_usd": float(sum(r["gross_usd"] for r in rows)),
            "signals": int(sum(r["funnel"]["signals"] for r in rows)),
            "taken": int(sum(r["funnel"]["taken"] for r in rows)),
            "first_half_vs_second_half": _welch(all_pcts[len(all_pcts) // 2:], all_pcts[: len(all_pcts) // 2])
            if len(all_pcts) >= 4 else {"t": None, "diff_pct": None, "verdict": "fewer than 4 trades"},
            "changes": int(sum(len(r["changes"]) for r in rows)),
            "promotions": int(sum(1 for r in rows for c in r["changes"] if c.get("promoted"))),
        })
        strategies[s] = {"days": rows, "overall": overall, "exit_lab": _exit_lab_for(s),
                         "posterior": _posterior(s), "in_roster": s in roster}

    # ── peers: rank each day by dollars and by per-trade percent ─────────────
    for d_i, d in enumerate(days):
        played = [(s, strategies[s]["days"][d_i]) for s in names if strategies[s]["days"][d_i]["n"]]
        by_usd = sorted(played, key=lambda x: -x[1]["net_usd"])
        by_pct = sorted(played, key=lambda x: -(x[1]["mean_net_pct"] or -1e9))
        for rank, (s, row) in enumerate(by_usd, 1):
            row["rank_usd"] = rank
        for rank, (s, row) in enumerate(by_pct, 1):
            row["rank_pct"] = rank
        for s in names:
            strategies[s]["days"][d_i]["peers_that_day"] = len(played)
    league = sorted(
        [{"strategy": s, **{k: v for k, v in strategies[s]["overall"].items()
                             if k not in ("first_half_vs_second_half",)},
          "in_roster": strategies[s]["in_roster"],
          "p_edge_above_hurdle": (strategies[s]["posterior"] or {}).get("p_edge_above_hurdle"),
          "status": (strategies[s]["posterior"] or {}).get("status")}
         for s in names],
        key=lambda r: -(r["net_usd"] or 0.0))
    for i, r in enumerate(league, 1):
        r["rank_usd"] = i
    for i, r in enumerate(sorted(league, key=lambda r: -(r["mean_net_pct"] if r["mean_net_pct"] is not None else -1e9)), 1):
        r["rank_pct"] = i
    desk_usd = float(sum(r["net_usd"] for r in league))
    for r in league:
        r["share_of_desk_pct"] = (100.0 * r["net_usd"] / desk_usd) if desk_usd else None

    return {
        "start": start, "end": end, "days": days, "mode": mode,
        "strategies": strategies, "league": league,
        "desk": {"net_usd": desk_usd, "trades": int(sum(r["n"] for r in league))},
        "generated_at": time.time(),
        "how_to_read": (
            "Per-trade net % is after both spreads, on the trade's own basis. Day rows carry n; "
            "with two trades a day the standard error is meaningless, so the comparisons that "
            "matter are the period columns and the Welch verdicts, which say 'not distinguishable' "
            "until there is enough to say otherwise. Rank is among strategies that traded that day, "
            "by dollars and separately by per-trade percent. The change log is what the desk "
            "recorded changing: refits, promotions, pauses, allocation moves, ideas that fired."),
    }
