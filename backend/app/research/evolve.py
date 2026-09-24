"""Accumulate evidence, and act on it only when there is enough to act on.

Two jobs, both automatic, both deliberately slow to conclude.

1. SHAPE MEMORY. Every day's report names the moves that cleared their own cost
   and that no strategy was watching, classified by shape. That list is
   accumulated here rather than read once and forgotten. When a shape has
   recurred on enough separate days AND produced enough coin-days, it is
   MEASURED automatically — train/test, the same discipline as everything else —
   and the verdict recorded. It is never shipped automatically: a rule built the
   moment a shape appears is how overfitting starts, and this project has three
   sessions of evidence for that.

2. LIVE TRAINING SET. Every closed trade is joined back to the signal that caused
   it, so the features at entry sit next to the realised outcome. Nothing can be
   fitted on 16 trades — but nothing can ever be fitted if the rows are not being
   collected, and until 2026-09-17 they were not: `trades.open_order_id` was NULL
   on every row, so no outcome could be traced to its inputs at all. The set is
   assembled here and reports how far it is from usable.

The honest threshold for refitting a ranking model on live data is roughly ten
observations per feature. day_climb carries six features, so ~60 clean trades
before anything is fitted, and a held-out slice of those before anything is
believed. That is weeks away, and saying so is the point.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import db

TZ = ZoneInfo("America/Chicago")

# A shape must recur this much before it is worth measuring at all.
MIN_DAYS_SEEN = 5
MIN_COIN_DAYS = 20
# Rows per feature before a live model may be fitted.
ROWS_PER_FEATURE = 10


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS shape_memory (
        shape TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
        best_pct REAL, round_trip_pct REAL, grade TEXT,
        PRIMARY KEY (shape, day, symbol))""")
    db.execute("""CREATE TABLE IF NOT EXISTS shape_verdicts (
        shape TEXT PRIMARY KEY, measured_at REAL NOT NULL,
        coin_days INTEGER, days_seen INTEGER, verdict TEXT, detail_json TEXT)""")


def absorb_day(day: str) -> dict:
    """Record what nothing was watching on this day, by shape."""
    ensure_schema()
    row = db.query_one("SELECT summary_json FROM daily_reports WHERE day=?", (day,))
    if not row or not row["summary_json"]:
        return {"day": day, "absorbed": 0}
    try:
        rep = json.loads(row["summary_json"])
    except Exception:
        return {"day": day, "absorbed": 0}
    n = 0
    for c in (rep.get("uncovered") or []) + (rep.get("uncatchable") or []):
        # Reports built before the shape classifier existed carry no shape. An
        # "unknown" bucket accumulates fast and says nothing, so it is skipped
        # rather than allowed to cross a threshold on its own.
        if not c.get("shape") or c["shape"] == "unknown":
            continue
        db.execute(
            """INSERT OR REPLACE INTO shape_memory(shape, day, symbol, best_pct,
                                                   round_trip_pct, grade)
               VALUES (?,?,?,?,?,?)""",
            (c.get("shape") or "unknown", day, c["symbol"],
             c.get("best_from_open_pct"), c.get("round_trip_pct"), c.get("grade")))
        n += 1
    return {"day": day, "absorbed": n}


def standings() -> list[dict]:
    """Which shapes are accumulating, and whether any is ready to measure."""
    ensure_schema()
    rows = db.query(
        """SELECT shape, COUNT(*) coin_days, COUNT(DISTINCT day) days_seen,
                  AVG(best_pct) avg_best, MAX(best_pct) max_best
           FROM shape_memory WHERE shape != 'unknown'
           GROUP BY shape ORDER BY coin_days DESC""")
    done = {r["shape"]: r for r in db.query("SELECT * FROM shape_verdicts")}
    out = []
    for r in rows:
        d = dict(r)
        v = done.get(d["shape"])
        d["already_measured"] = bool(v)
        d["verdict"] = v["verdict"] if v else None
        d["ready_to_measure"] = (not v
                                 and d["days_seen"] >= MIN_DAYS_SEEN
                                 and d["coin_days"] >= MIN_COIN_DAYS)
        d["needs"] = (f"{max(0, MIN_DAYS_SEEN - d['days_seen'])} more days, "
                      f"{max(0, MIN_COIN_DAYS - d['coin_days'])} more coin-days"
                      if not d["ready_to_measure"] and not v else None)
        out.append(d)
    return out


def record_verdict(shape: str, verdict: str, detail: dict) -> None:
    ensure_schema()
    s = next((x for x in standings() if x["shape"] == shape), {})
    db.execute(
        """INSERT OR REPLACE INTO shape_verdicts(shape, measured_at, coin_days,
                                                 days_seen, verdict, detail_json)
           VALUES (?,?,?,?,?,?)""",
        (shape, time.time(), s.get("coin_days"), s.get("days_seen"),
         verdict, json.dumps(detail)))
    db.log_event("INFO", "evolve", f"shape '{shape}' measured: {verdict}")


# ── the live training set ────────────────────────────────────────────────────
def training_set(strategy: str | None = None, mode: str = "paper") -> dict:
    """Every closed trade joined to the features that caused it."""
    sql = """SELECT t.id, t.symbol, t.strategy, t.net_pnl_usd, t.gross_pnl_usd,
                    t.qty, t.entry_px, t.holding_s, t.ts_open, s.features_json
             FROM trades t
             LEFT JOIN orders o ON o.id = t.open_order_id
             LEFT JOIN signals s ON s.id = o.signal_id
             WHERE t.mode = ?"""
    args: list = [mode]
    if strategy:
        sql += " AND t.strategy = ?"
        args.append(strategy)
    rows = db.query(sql + " ORDER BY t.ts_close", args)

    from app.feedback import defects as _def
    usable, orphans = [], 0
    feature_names: set[str] = set()
    for r in rows:
        d = dict(r)
        if _def.is_excluded(d):
            continue
        if not d.get("features_json"):
            orphans += 1
            continue
        try:
            feats = json.loads(d["features_json"]) or {}
        except Exception:
            orphans += 1
            continue
        notional = abs(float(d["qty"] or 0) * float(d["entry_px"] or 0))
        if notional <= 0:
            continue
        num = {k: v for k, v in feats.items()
               if isinstance(v, (int, float)) and v is not None}
        feature_names |= set(num)
        usable.append({
            "trade_id": d["id"], "symbol": d["symbol"], "strategy": d["strategy"],
            "features": num,
            "net_pct": float(d["net_pnl_usd"] or 0) / notional * 100.0,
            "hour": datetime.fromtimestamp(float(d["ts_open"]), TZ).hour,
        })

    n_feat = len(feature_names)
    need = n_feat * ROWS_PER_FEATURE
    return {
        "strategy": strategy or "all",
        "rows": len(usable),
        "orphans_no_features": orphans,
        "features": sorted(feature_names),
        "rows_needed_to_fit": need,
        "ready_to_fit": len(usable) >= need and n_feat > 0,
        "shortfall": max(0, need - len(usable)),
        "mean_net_pct": round(statistics.fmean([u["net_pct"] for u in usable]), 4)
                        if usable else None,
        "note": (
            f"{len(usable)} clean rows against {n_feat} features; a live fit needs about "
            f"{need} ({ROWS_PER_FEATURE} per feature) before it is anything but noise."
            if n_feat else
            "No trade can be joined to its signal yet. Trades opened before "
            "2026-09-17 have no open_order_id, so their features are unrecoverable."),
    }


def status() -> dict:
    return {
        "shapes": standings(),
        "training": [training_set(s) for s in
                     ("day_climb", "morning_dip", "pump_ride", "volume_build")],
        "thresholds": {"min_days_seen": MIN_DAYS_SEEN, "min_coin_days": MIN_COIN_DAYS,
                       "rows_per_feature": ROWS_PER_FEATURE},
    }
