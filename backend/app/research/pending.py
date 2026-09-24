"""Ideas worth trying that do not have enough data yet — and what wakes them up.

    "Is this something that you will put in automatic activation when enough data
     is aggregated? If yes do it and also in general to remember to automatically
     do this approach for things that we want to try but need to do later when
     have gathered enough data."

The pattern kept recurring: a good idea gets deferred for want of evidence, the
reason is written in a chat message, and nothing ever comes back to it. This is
the register that comes back to it.

Each entry names the idea, the exact quantity it is waiting on, how much of that
exists today, and what happens the moment it is enough. The scheduler checks
every few hours. Nothing here promotes itself into live trading — reaching the
threshold triggers the MEASUREMENT, and promotion still has to clear the usual
two-standard-error bar against the incumbent.

Adding one is three lines: a name, a readiness function, and what to do when
ready. That is deliberate — an idea that is hard to register is an idea that
quietly gets dropped.
"""
from __future__ import annotations

import time

from app.core import db

KEY = "pending_experiment"


def ensure_schema() -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS pending_experiments (
            name       TEXT PRIMARY KEY,
            first_seen REAL NOT NULL,
            fired_at   REAL,
            outcome    TEXT
        )""")


# ─────────────────────────── readiness measures ─────────────────────────────

def _clean_trades() -> int:
    from app.feedback import defects
    rows = db.query("SELECT ts_close, strategy FROM trades WHERE mode='paper'")
    return sum(1 for r in rows if not defects.is_excluded(dict(r)))


def _replayed_trades() -> int:
    r = db.query_one("SELECT COUNT(DISTINCT trade_id) AS n FROM exit_counterfactuals")
    return int((r["n"] if r else 0) or 0)


def _strategy_with_positive_edge() -> int:
    """How many strategies have a measured edge whose lower bound clears zero."""
    n = 0
    for row in db.query("SELECT strategy, posterior_json FROM strategy_state "
                        "GROUP BY strategy HAVING MAX(ts)"):
        try:
            import json
            p = json.loads(row["posterior_json"] or "{}")
            lo = p.get("edge_ci_low_bps")
            if lo is not None and float(lo) > 0:
                n += 1
        except Exception:
            continue
    return n


# ──────────────────────────── the register ──────────────────────────────────

EXPERIMENTS = [
    {
        "name": "kelly_sizing",
        "idea": "Size by fractional Kelly instead of equal risk",
        "waiting_for": "a strategy whose measured edge is positive with the "
                       "confidence interval clear of zero",
        "measure": _strategy_with_positive_edge,
        "need": 1,
        "unit": "strategies with a proven edge",
        "why_not_yet": ("Kelly sizes from an edge. With no strategy whose edge is "
                        "distinguishable from zero, Kelly would be sizing from noise "
                        "and would bet hardest on whichever strategy got luckiest."),
        "on_ready": ("Fit the Kelly fraction from that strategy's own trade "
                     "distribution, cap it at quarter-Kelly, and run it as a "
                     "challenger against equal-risk sizing."),
    },
    {
        "name": "exit_rule_promotion",
        "idea": "Replace the live exit rule with whichever the lab has proven better",
        "waiting_for": "replayed closed trades",
        "measure": _replayed_trades,
        "need": 40,
        "unit": "trades replayed in the exit lab",
        "why_not_yet": ("On 27 trades the live sample and the four-year study "
                        "disagree about the breakeven floor. Choosing between them "
                        "now would be picking the one that agrees with us."),
        "on_ready": ("Run the leading rule as a formal challenger through "
                     "research/retrain.py: same bars, two standard errors, beat "
                     "every earlier version or stay where you are."),
    },
    {
        "name": "live_feature_retrain",
        "idea": "Refit entry rules on the desk's own live trades",
        "waiting_for": "clean trades linked to their features",
        "measure": _clean_trades,
        "need": 60,
        "unit": "clean linked trades",
        "why_not_yet": ("Six features need roughly ten rows each before a fit is "
                        "anything but memorisation of the sample."),
        "on_ready": ("Hand the assembled training set to research/retrain.py as a "
                     "challenger."),
    },
    {
        "name": "scale_out_live",
        "idea": "Take part of a position off into strength, trail the rest",
        "waiting_for": "replayed trades where the scale-out trigger actually fired",
        "measure": _replayed_trades,
        "need": 40,
        "unit": "trades replayed in the exit lab",
        "why_not_yet": ("The protection against the known trap — selling the winning "
                        "part and holding the losing part — is built, but 27 trades "
                        "cannot tell a real improvement from a lucky one."),
        "on_ready": ("Promote the leading scale-out rule through the same "
                     "challenger bar as any other exit change."),
    },
]


def status() -> dict:
    """Where every deferred idea stands, and how far from waking up."""
    ensure_schema()
    fired = {r["name"]: r for r in db.query("SELECT * FROM pending_experiments")}
    out = []
    for e in EXPERIMENTS:
        try:
            have = int(e["measure"]())
        except Exception:
            have = 0
        row = fired.get(e["name"])
        ready = have >= e["need"]
        out.append({
            "name": e["name"], "idea": e["idea"],
            "waiting_for": e["waiting_for"], "why_not_yet": e["why_not_yet"],
            "on_ready": e["on_ready"],
            "have": have, "need": e["need"], "unit": e["unit"],
            "short_by": max(0, e["need"] - have),
            "progress": min(1.0, have / e["need"]) if e["need"] else 1.0,
            "ready": ready,
            "fired_at": (row["fired_at"] if row else None),
            "outcome": (row["outcome"] if row else None),
        })
    out.sort(key=lambda r: -r["progress"])
    return {"experiments": out,
            "n_ready": sum(1 for r in out if r["ready"] and not r["fired_at"]),
            "rule": ("Reaching a threshold triggers the MEASUREMENT, never a live "
                     "change. Promotion still has to beat the incumbent by two "
                     "standard errors on data it has not seen.")}


def fire_ready() -> list[dict]:
    """Record every experiment whose evidence has arrived, once."""
    ensure_schema()
    done = []
    for e in status()["experiments"]:
        if not e["ready"] or e["fired_at"]:
            continue
        db.execute(
            "INSERT INTO pending_experiments(name, first_seen, fired_at, outcome) "
            "VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "fired_at=excluded.fired_at, outcome=excluded.outcome",
            (e["name"], time.time(), time.time(),
             f"threshold reached: {e['have']} {e['unit']}"))
        db.log_event("INFO", "pending",
                     f"'{e['idea']}' now has the data it was waiting for "
                     f"({e['have']} {e['unit']}). Next: {e['on_ready']}")
        done.append(e)
    return done
