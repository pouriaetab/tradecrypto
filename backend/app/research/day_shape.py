"""When a coin's day has turned bear, what does the rest of that day do?

    "today zec['s] primary trend is down bear ... indeed it keeps going down,
     so for these is it better to cut the losses sooner ... why can't we go
     back in time and accumulate zec and many other ones that had a bear day"

This is that: every coin-day in the hourly history, read at fixed local hours,
classified by the SHAPE of the day so far, and followed to the day's close.

The shape is structural, with no percentage in it:

    bear at hour h  =  close below the day's open
                       AND lower highs over the last W hours than the W before
                       AND lower lows over the last W hours than the W before

For each (hour, window) the study reports, for bear days and for the rest:
how many coin-days, the average and median rest-of-day return, how often the
rest of the day was negative, and the average further drawdown before the
close. If a bear day's rest-of-day return is reliably negative and the
non-bear rest is not, cutting a position when the shape appears is the right
exit; if the two are alike, the shape carries no information and cutting is
just paying the spread twice. The exit lab's `bear_cut_*` rules are the same
test on the trades the desk actually took; this is the same test on every day
the venue has, so the sample is thousands instead of dozens.

No look-ahead: the shape at hour h uses bars up to h; the label uses bars
after h. The day is the operator's (America/Chicago).
"""
from __future__ import annotations

import datetime as _dt
import json
import time
import warnings
from zoneinfo import ZoneInfo

import numpy as np

from app.core import db

TZ = ZoneInfo("America/Chicago")
READ_HOURS = (6, 9, 12)
WINDOWS_H = (3, 6)


def _summ(vals: list[float], dd: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    a = np.asarray(vals, dtype=float)
    d = np.asarray(dd, dtype=float) if dd else np.zeros(0)
    return {"n": int(a.size), "mean_rest_pct": float(a.mean()),
            "median_rest_pct": float(np.median(a)),
            "se_pct": float(a.std(ddof=1) / np.sqrt(a.size)) if a.size > 1 else 0.0,
            "p_negative": float((a < 0).mean() * 100.0),
            "mean_further_drawdown_pct": float(d.mean()) if d.size else None}


def study(panel=None, days: int = 4 * 365) -> dict:
    from app.execution import engine
    from app.data import selection

    t0 = time.time()
    if panel is None:
        panel = engine.build_panel(refresh=False, granularity=3600, limit=days * 24 + 48)
    if panel.T < 48 * 10:
        return {"available": False, "why": f"only {panel.T} hourly bars"}
    keep = set(selection.tradeable_symbols()) | set(getattr(selection, "CORE", []))
    cols = [i for i, s in enumerate(panel.symbols) if s in keep] or list(range(panel.N))

    local = [_dt.datetime.fromtimestamp(float(t), TZ) for t in panel.ts]
    dates = np.array([d.date().toordinal() for d in local])
    hours = np.array([d.hour for d in local])

    out: dict[str, dict] = {}
    buckets: dict[tuple, dict[str, list]] = {}
    coin_days = 0
    day_ids = np.unique(dates)
    _w = warnings.catch_warnings()       # all-NaN slices are "no bars", not a warning
    _w.__enter__()
    warnings.simplefilter("ignore", category=RuntimeWarning)
    for day in day_ids:
        idx = np.where(dates == day)[0]
        if idx.size < 20:                      # a partial day at either end
            continue
        hi = panel.high[idx][:, cols]; lo = panel.low[idx][:, cols]
        cl = panel.close[idx][:, cols]; hrs = hours[idx]
        day_open = cl[0]
        last = cl[-1]
        for h in READ_HOURS:
            at = np.where(hrs == h)[0]
            if at.size == 0:
                continue
            i = int(at[0])
            if i < 1:
                continue
            for w in WINDOWS_H:
                if i + 1 < 2 * w:
                    continue
                recent_h = np.nanmax(hi[i - w + 1:i + 1], axis=0)
                prior_h = np.nanmax(hi[i - 2 * w + 1:i - w + 1], axis=0)
                recent_l = np.nanmin(lo[i - w + 1:i + 1], axis=0)
                prior_l = np.nanmin(lo[i - 2 * w + 1:i - w + 1], axis=0)
                bear = (cl[i] < day_open) & (recent_h < prior_h) & (recent_l < prior_l)
                rest = (last / cl[i] - 1.0) * 100.0
                # further drawdown from here to the close, as a negative number
                future_low = np.nanmin(lo[i + 1:], axis=0) if i + 1 < cl.shape[0] else cl[i]
                dd = np.minimum(0.0, (future_low / cl[i] - 1.0) * 100.0)
                ok = np.isfinite(rest) & np.isfinite(cl[i]) & (cl[i] > 0) & np.isfinite(day_open)
                for flag, name in ((bear, "bear"), (~bear, "not_bear")):
                    m = ok & flag
                    if not m.any():
                        continue
                    b = buckets.setdefault((h, w, name), {"rest": [], "dd": []})
                    b["rest"].extend(rest[m].tolist())
                    b["dd"].extend(dd[m].tolist())
        coin_days += len(cols)

    _w.__exit__(None, None, None)
    for (h, w, name), b in buckets.items():
        out.setdefault(f"h{h:02d}_w{w}", {})[name] = _summ(b["rest"], b["dd"])
    # the one line each cell is for: cut vs hold, on bear days. The scale that
    # makes a difference MEANINGFUL is the venue's own one-side spread: a
    # rest-of-day expectation smaller than that is not a reason to trade.
    try:
        from app.execution import rh_spread
        side_pct = float(rh_spread.DEFAULT_SPREAD_PCT)
    except Exception:
        side_pct = 0.95
    for key, cell in out.items():
        bear, rest = cell.get("bear", {}), cell.get("not_bear", {})
        if bear.get("n") and rest.get("n"):
            diff = bear["mean_rest_pct"] - rest["mean_rest_pct"]
            se = float(np.hypot(bear["se_pct"], rest["se_pct"]))
            cell["bear_minus_not_pct"] = diff
            cell["sigmas"] = diff / se if se else 0.0
            cell["scale_pct"] = side_pct
            m = bear["mean_rest_pct"]
            cell["verdict"] = (
                "bear days keep falling by more than a spread — cutting is right"
                if m <= -side_pct and cell["sigmas"] <= -2 else
                "bear days do measurably worse, but the expected rest-of-day move is smaller "
                "than one side of the spread — the shape alone does not pay for an exit"
                if cell["sigmas"] <= -2 else
                "bear days recover as often as not — the shape carries no information")
    return {"available": True, "cells": out, "hours": list(READ_HOURS),
            "windows_h": list(WINDOWS_H), "coins": len(cols),
            "days": int(len(day_ids)), "took_s": time.time() - t0,
            "definition": ("bear at hour h = close below the day's open AND lower highs AND "
                           "lower lows over the last W hours than the W hours before. "
                           "Rest-of-day = the day's last close against the price at hour h, "
                           "before costs. America/Chicago day.")}


def record(res: dict) -> None:
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "day_shape", "bear-day rest-of-day study",
                json.dumps({"hours": READ_HOURS, "windows_h": WINDOWS_H}),
                json.dumps(res), "; ".join(f"{k}: {v.get('verdict', '')}" for k, v in res.get("cells", {}).items())[:400]))


def latest() -> dict | None:
    row = db.query_one("SELECT ts, result_json FROM runs WHERE kind='day_shape' ORDER BY ts DESC LIMIT 1")
    if not row:
        return None
    d = json.loads(row["result_json"])
    d["ran_at"] = row["ts"]
    return d
