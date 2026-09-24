"""Does the same trigger on a rolling 60-minute window fire earlier than the
calendar hour -- and does it fire on more junk?

    "the same trigger on a rolling 60-min window ... replayed over the history
     against the calendar-hour version. If it fires earlier without more false
     entries, promote it."

Two strategies, one rule: volume_build (hourly bars) and pump_catch (the same
parameters on 15-minute bars). Both are run over the same span, both entry sets
are then given the IDENTICAL exit (volume_build's own: +round-trip+margin
target, catastrophe stop, 36h give-up) simulated on the 15-minute bars, with
Robinhood's spread charged both sides. So the only thing that differs between
the two rows is the clock.

What is compared
----------------
  * matched pairs   an hourly entry and a rolling entry in the same coin within
                    the same two hours: how many minutes earlier the rolling one
                    was, and how much lower it bought
  * extra entries   rolling entries with no hourly twin: the "false entries"
                    the operator asked about, scored with the same exit
  * missed entries  hourly entries with no rolling twin
  * per entry set   n, net %/trade after both spreads, win rate, standard error

Assumptions stated: the hourly rule is filled at the CLOSE of the hour whose
volume crossed (the live engine detects mid-hour on partial volume, so live sits
somewhere between the two rows); the rolling rule at the close of its 15-minute
bar. Stops are checked before targets inside a bar. No look-ahead: each rule
sees only bars up to the one it fires on.
"""
from __future__ import annotations

import json
import time

import numpy as np

from app.core import db

SIDE_FALLBACK = 0.0095


def _side(symbol: str) -> float:
    try:
        from app.execution import rh_spread
        return float(rh_spread.get(symbol)["spread_pct"]) / 100.0
    except Exception:
        return SIDE_FALLBACK


def _entries(strategy, panel, tradeable: set[str]) -> list[dict]:
    """Every signal the strategy would have emitted, bar by bar, no look-ahead."""
    out = []
    w = strategy.warmup_bars()
    for t in range(w, panel.T):
        try:
            sigs = strategy.generate(panel, t)
        except Exception:
            continue
        for s in sigs:
            if s.symbol not in tradeable:
                continue
            k = panel.symbols.index(s.symbol)
            out.append({"symbol": s.symbol, "t": t, "ts": float(panel.ts[t]),
                        "k": k, "px": float(panel.close[t, k]),
                        "target_bps": float(s.target_bps or 0.0),
                        "stop_bps": float(s.stop_bps or 0.0),
                        "hold_s": float(s.hold_seconds or 0.0)})
    return out


def _simulate(entry: dict, p15, start_idx: int, side: float) -> dict:
    """volume_build's exit on 15-minute bars from the bar AFTER entry."""
    k = entry["k"]
    e = entry["px"] * (1.0 + side)                  # buy above the mid
    tgt = entry["px"] * (1.0 + entry["target_bps"] / 1e4)
    stp = entry["px"] * (1.0 - entry["stop_bps"] / 1e4) if entry["stop_bps"] else None
    deadline = entry["ts"] + entry["hold_s"]
    hi, lo, cl, ts = p15.high[:, k], p15.low[:, k], p15.close[:, k], p15.ts
    for i in range(start_idx, p15.T):
        if ts[i] > deadline:
            px = cl[i - 1] if i > 0 else cl[i]
            return {"net_pct": (px * (1 - side) / e - 1) * 100, "reason": "time",
                    "hours": (ts[i] - entry["ts"]) / 3600}
        if stp is not None and np.isfinite(lo[i]) and lo[i] <= stp:
            return {"net_pct": (stp * (1 - side) / e - 1) * 100, "reason": "stop",
                    "hours": (ts[i] - entry["ts"]) / 3600}
        if np.isfinite(hi[i]) and hi[i] >= tgt:
            return {"net_pct": (tgt * (1 - side) / e - 1) * 100, "reason": "target",
                    "hours": (ts[i] - entry["ts"]) / 3600}
    px = cl[p15.T - 1]
    return {"net_pct": (px * (1 - side) / e - 1) * 100, "reason": "open",
            "hours": (ts[p15.T - 1] - entry["ts"]) / 3600}


def _stats(rows: list[dict]) -> dict:
    v = [r["net_pct"] for r in rows if r.get("net_pct") is not None and np.isfinite(r["net_pct"])]
    n = len(v)
    if not n:
        return {"n": 0}
    mean = float(np.mean(v))
    sd = float(np.std(v, ddof=1)) if n > 1 else 0.0
    mix: dict[str, int] = {}
    for r in rows:
        mix[r.get("reason", "?")] = mix.get(r.get("reason", "?"), 0) + 1
    return {"n": n, "mean_net_pct": mean, "se_pct": sd / np.sqrt(n),
            "win_rate": 100.0 * sum(1 for x in v if x > 0) / n,
            "median_hours": float(np.median([r["hours"] for r in rows])),
            "exit_mix": mix}


def compare(days: int = 365, hourly_panel=None, p15=None) -> dict:
    from app.execution import engine
    from app.strategy.registry import build
    from app.data import selection

    t0 = time.time()
    tradeable = set(selection.tradeable_symbols()) | set(getattr(selection, "CORE", []))
    hourly = build("volume_build")
    rolling = build("pump_catch")
    if hourly_panel is None:
        hourly_panel = engine.build_panel(refresh=False, granularity=3600,
                                          limit=days * 24 + hourly.warmup_bars() + 48)
    if p15 is None:
        p15 = engine.build_panel(refresh=False, granularity=900,
                                 limit=days * 96 + rolling.warmup_bars() + 192)
    if p15.T < rolling.warmup_bars() + 96 or hourly_panel.T < hourly.warmup_bars() + 24:
        return {"available": False,
                "why": f"not enough bars: hourly {hourly_panel.T}, 15-min {p15.T}"}

    # Common span only, so neither clock gets extra history to fire on.
    span_start = max(float(hourly_panel.ts[hourly.warmup_bars()]),
                     float(p15.ts[rolling.warmup_bars()]))
    span_end = min(float(hourly_panel.ts[-1]) + 3600.0, float(p15.ts[-1]) + 900.0)

    h_ev = [e for e in _entries(hourly, hourly_panel, tradeable) if span_start <= e["ts"] < span_end]
    r_ev = [e for e in _entries(rolling, p15, tradeable) if span_start <= e["ts"] < span_end]

    # Non-overlapping per coin, as the book trades: one position per coin at a
    # time, the next entry only after the previous one has exited.
    idx15 = {float(ts): i for i, ts in enumerate(p15.ts)}
    ts15 = p15.ts
    sides = {s: _side(s) for s in tradeable}

    def _score(events: list[dict], close_offset_s: float) -> list[dict]:
        busy_until: dict[str, float] = {}
        out = []
        for e in sorted(events, key=lambda x: x["ts"]):
            fill_ts = e["ts"] + close_offset_s              # bar close
            if busy_until.get(e["symbol"], -1.0) > fill_ts:
                continue
            # entry price = the 15-min close at the fill time
            j = int(np.searchsorted(ts15, fill_ts - 900.0))
            if j >= p15.T or j < 0:
                continue
            px = float(p15.close[j, e["k15"]])
            if not np.isfinite(px) or px <= 0:
                continue
            ent = {**e, "px": px, "k": e["k15"], "ts": fill_ts}
            res = _simulate(ent, p15, j + 1, sides[e["symbol"]])
            busy_until[e["symbol"]] = fill_ts + res["hours"] * 3600.0
            out.append({**ent, **res, "fill_ts": fill_ts})
        return out

    k15 = {s: i for i, s in enumerate(p15.symbols)}
    for e in h_ev:
        e["k15"] = k15.get(e["symbol"])
    for e in r_ev:
        e["k15"] = k15.get(e["symbol"])
    h_ev = [e for e in h_ev if e["k15"] is not None]
    r_ev = [e for e in r_ev if e["k15"] is not None]

    h_rows = _score(h_ev, 3600.0)
    r_rows = _score(r_ev, 900.0)

    # Matching: a rolling fill within the two hours ending at the hourly fill.
    r_by_sym: dict[str, list] = {}
    for r in r_rows:
        r_by_sym.setdefault(r["symbol"], []).append(r)
    matched, missed = [], []
    used = set()
    for h in h_rows:
        cands = [r for r in r_by_sym.get(h["symbol"], [])
                 if id(r) not in used and h["fill_ts"] - 7200.0 < r["fill_ts"] <= h["fill_ts"]]
        if cands:
            r = max(cands, key=lambda x: x["fill_ts"])
            used.add(id(r))
            matched.append({"symbol": h["symbol"], "hourly_ts": h["fill_ts"],
                            "rolling_ts": r["fill_ts"],
                            "lead_min": (h["fill_ts"] - r["fill_ts"]) / 60.0,
                            "price_improvement_pct": (h["px"] / r["px"] - 1.0) * 100.0,
                            "hourly_net_pct": h["net_pct"], "rolling_net_pct": r["net_pct"]})
        else:
            missed.append(h)
    extra = [r for r in r_rows if id(r) not in used]

    leads = [m["lead_min"] for m in matched]
    impr = [m["price_improvement_pct"] for m in matched]
    out = {
        "available": True,
        "span_days": (span_end - span_start) / 86400.0,
        "coins": len(tradeable),
        "hourly": _stats(h_rows),
        "rolling": _stats(r_rows),
        "matched": {
            "n": len(matched),
            "median_lead_min": float(np.median(leads)) if leads else None,
            "mean_lead_min": float(np.mean(leads)) if leads else None,
            "median_price_improvement_pct": float(np.median(impr)) if impr else None,
            "hourly_net_on_matched": _stats([{"net_pct": m["hourly_net_pct"], "reason": "-", "hours": 0} for m in matched]),
            "rolling_net_on_matched": _stats([{"net_pct": m["rolling_net_pct"], "reason": "-", "hours": 0} for m in matched]),
        },
        "extra_rolling_entries": _stats(extra),
        "hourly_entries_rolling_missed": _stats(missed),
        "took_s": time.time() - t0,
        "assumptions": ("hourly rule filled at the close of the hour that crossed; rolling "
                        "at the close of its 15-minute bar; same exit for both, simulated "
                        "on 15-minute bars with the stop checked before the target; "
                        "Robinhood's measured spread charged both sides; one position per "
                        "coin at a time"),
    }
    d = out["rolling"].get("mean_net_pct"); h = out["hourly"].get("mean_net_pct")
    if d is not None and h is not None:
        se = float(np.hypot(out["rolling"].get("se_pct", 0.0), out["hourly"].get("se_pct", 0.0)))
        out["rolling_minus_hourly_pct"] = d - h
        out["sigmas"] = (d - h) / se if se else 0.0
        ex = out["extra_rolling_entries"]
        out["verdict"] = (
            "rolling fires earlier AND its extra entries are not worse -- promote"
            if leads and np.median(leads) > 0 and ex.get("n", 0) and
               ex.get("mean_net_pct", -1e9) >= h - 1e-9 and out["sigmas"] > -1
            else "rolling fires earlier but its extra entries lose more -- do not promote on this alone"
            if leads and np.median(leads) > 0
            else "no lead measured -- nothing to promote")
    return out


def record(res: dict) -> None:
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "rolling_entry", "pump_catch vs volume_build",
                json.dumps({"days": 365}), json.dumps(res), res.get("verdict", "n/a")))


def latest() -> dict | None:
    row = db.query_one("SELECT ts, result_json FROM runs WHERE kind='rolling_entry' ORDER BY ts DESC LIMIT 1")
    if not row:
        return None
    d = json.loads(row["result_json"])
    d["ran_at"] = row["ts"]
    return d
