"""Per-strategy P&L ledger.

Every strategy keeps its own book. That matters more than it sounds: a combined
equity curve hides the case where one strategy is quietly paying for another's
losses, which is exactly the situation where you want to defund something.

All day boundaries are the operator's trading day — midnight to 23:59 local
(America/Chicago), not UTC — because that is how he thinks about a day and how
the daily targets and limits are framed.

Everything here reads from the trades table. Nothing is recomputed from prices,
so the ledger cannot disagree with what the engine actually did.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core import clock, db
from app.research import stats as S

# A hardcoded -6.0 was wrong for eight months of the year. Chicago is UTC-5 from
# March to November, so every day boundary here sat an hour off the ones used by
# guards._today_start, budget._local_day_bounds and daily_report._bounds — a
# trade closing at 00:30 CDT landed in yesterday here and today everywhere else.
# clock.TZ is the single definition; `clock` was already imported and unused.


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=clock.TZ).strftime("%Y-%m-%d")


def day_bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, tzinfo=clock.TZ)
    # timedelta(days=1), not +86400: a Chicago day is 23 or 25 hours twice a year.
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _trades(strategy: str | None, mode: str, start_ts: float | None,
            end_ts: float | None) -> list[dict]:
    sql = "SELECT * FROM trades WHERE mode=?"
    params: list = [mode]
    if strategy:
        sql += " AND strategy=?"
        params.append(strategy)
    if start_ts:
        sql += " AND ts_close >= ?"
        params.append(start_ts)
    if end_ts:
        sql += " AND ts_close <= ?"
        params.append(end_ts)
    return db.query(sql + " ORDER BY ts_close ASC", params)


def strategy_ledger(strategy: str, mode: str = "paper",
                    start_ts: float | None = None, end_ts: float | None = None) -> dict:
    """One strategy's complete book over a date range."""
    trades = _trades(strategy, mode, start_ts, end_ts)
    if not trades:
        return {"strategy": strategy, "mode": mode, "n_trades": 0,
                "note": ("no closed trades in this range. In paper mode the engine has to "
                         "be running and a strategy has to be claiming an edge before "
                         "anything appears here.")}

    cum, equity_series = 0.0, []
    rets = []
    for t in trades:
        cum += t["net_pnl_usd"]
        equity_series.append({"ts": t["ts_close"], "cumulative_net_usd": cum,
                              "symbol": t["symbol"], "net_usd": t["net_pnl_usd"]})
        notional = abs(t["qty"] * t["entry_px"])
        if notional > 0:
            rets.append(t["net_pnl_usd"] / notional)

    by_day: dict[str, dict] = defaultdict(
        lambda: {"gross_usd": 0.0, "cost_usd": 0.0, "net_usd": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        d = by_day[local_day(t["ts_close"])]
        d["gross_usd"] += t["gross_pnl_usd"]
        d["cost_usd"] += t["cost_usd"]
        d["net_usd"] += t["net_pnl_usd"]
        d["trades"] += 1
        d["wins"] += 1 if t["net_pnl_usd"] > 0 else 0
    daily = [{"day": k, **v, "hit_rate": v["wins"] / v["trades"]} for k, v in sorted(by_day.items())]

    by_symbol: dict[str, dict] = defaultdict(lambda: {"net_usd": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        s = by_symbol[t["symbol"]]
        s["net_usd"] += t["net_pnl_usd"]
        s["trades"] += 1
        s["wins"] += 1 if t["net_pnl_usd"] > 0 else 0
    symbols = sorted(({"symbol": k, **v, "hit_rate": v["wins"] / v["trades"]}
                      for k, v in by_symbol.items()),
                     key=lambda r: r["net_usd"], reverse=True)

    total_gross = sum(t["gross_pnl_usd"] for t in trades)
    total_cost = sum(t["cost_usd"] for t in trades)
    total_net = sum(t["net_pnl_usd"] for t in trades)
    holds = [t["holding_s"] / 60 for t in trades]

    summary = S.performance_summary(rets, periods_per_year=365 * 24) if len(rets) >= 2 else {
        "n": len(rets), "sufficient": False}

    return {
        "strategy": strategy, "mode": mode,
        "range": {"start_ts": start_ts, "end_ts": end_ts,
                  "first_trade": trades[0]["ts_close"], "last_trade": trades[-1]["ts_close"]},
        "n_trades": len(trades),
        "totals": {
            "gross_usd": total_gross, "cost_usd": total_cost, "net_usd": total_net,
            "cost_share_of_gross": (total_cost / abs(total_gross)) if total_gross else None,
        },
        "equity_series": equity_series,
        "daily": daily,
        "by_symbol": symbols,
        "holding_minutes": {
            "median": float(np.median(holds)) if holds else None,
            "min": float(np.min(holds)) if holds else None,
            "max": float(np.max(holds)) if holds else None,
        },
        "performance": summary,
        "trades": trades[-200:],
        "reading_note": (
            "cost_share_of_gross is the number to watch. If costs are eating more than "
            "about half of gross P&L, the strategy is trading too often for the size of "
            "the moves it catches, whatever the net figure says."
        ),
    }


def compare(mode: str = "paper", start_ts: float | None = None,
            end_ts: float | None = None) -> dict:
    """All strategies side by side over the same window."""
    names = [r["strategy"] for r in db.query(
        "SELECT DISTINCT strategy FROM trades WHERE mode=?", (mode,))]
    rows = []
    for n in names:
        trades = _trades(n, mode, start_ts, end_ts)
        if not trades:
            continue
        net = sum(t["net_pnl_usd"] for t in trades)
        gross = sum(t["gross_pnl_usd"] for t in trades)
        cost = sum(t["cost_usd"] for t in trades)
        wins = sum(1 for t in trades if t["net_pnl_usd"] > 0)
        rets = [t["net_pnl_usd"] / abs(t["qty"] * t["entry_px"])
                for t in trades if t["qty"] and t["entry_px"]]
        _, lo, hi = S.bootstrap_ci(rets, np.mean, n_boot=1500) if len(rets) >= 5 else (0, float("nan"), float("nan"))
        rows.append({
            "strategy": n, "trades": len(trades), "net_usd": net,
            "gross_usd": gross, "cost_usd": cost,
            "hit_rate": wins / len(trades),
            "mean_bps": float(np.mean(rets) * 1e4) if rets else None,
            "mean_ci_bps": [lo * 1e4, hi * 1e4],
            "positive_at_95": bool(np.isfinite(lo) and lo > 0),
            "median_hold_min": float(np.median([t["holding_s"] / 60 for t in trades])),
        })
    rows.sort(key=lambda r: r["net_usd"], reverse=True)
    return {
        "mode": mode, "rows": rows,
        "note": ("'positive_at_95' is the only column that means anything before ~30 trades. "
                 "Net dollars on a handful of trades is noise wearing a dollar sign."),
    }


def daily_pnl(mode: str = "paper", days: int = 30) -> dict:
    """Day-by-day P&L across all strategies, on the operator's day boundary."""
    since = time.time() - days * 86400
    trades = _trades(None, mode, since, None)
    by_day: dict[str, dict] = defaultdict(
        lambda: {"net_usd": 0.0, "gross_usd": 0.0, "cost_usd": 0.0, "trades": 0,
                 "by_strategy": defaultdict(float)})
    for t in trades:
        d = by_day[local_day(t["ts_close"])]
        d["net_usd"] += t["net_pnl_usd"]
        d["gross_usd"] += t["gross_pnl_usd"]
        d["cost_usd"] += t["cost_usd"]
        d["trades"] += 1
        d["by_strategy"][t["strategy"]] += t["net_pnl_usd"]
    out = []
    cum = 0.0
    for day in sorted(by_day):
        v = by_day[day]
        cum += v["net_usd"]
        out.append({"day": day, **{k: v[k] for k in ("net_usd", "gross_usd", "cost_usd", "trades")},
                    "cumulative_net_usd": cum, "by_strategy": dict(v["by_strategy"])})
    nets = [d["net_usd"] for d in out]
    return {
        "mode": mode, "days": out,
        "summary": {
            "days_traded": len(out),
            "total_net_usd": cum,
            "best_day_usd": max(nets) if nets else None,
            "worst_day_usd": min(nets) if nets else None,
            "green_days": sum(1 for n in nets if n > 0),
            "avg_day_usd": float(np.mean(nets)) if nets else None,
        },
        "day_boundary": "midnight to 23:59 America/Chicago",
    }
