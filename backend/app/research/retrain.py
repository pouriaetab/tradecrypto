"""Retrain a strategy, then make it earn its place against the one it replaces.

WHAT THIS IS FOR
----------------
    "do the accumulation automatically so when it reaches certain threshold it
     automatically triggers models retraining testing validating etc and running
     back on old data to see how the new model performs in comparison to the old
     one/s"

Until now `evolve.py` accumulated evidence and stopped at "ready to measure".
This is the part that measures, and the part that refuses.

THE PIPELINE, AND WHY EACH STEP IS THERE
----------------------------------------
1. TRIGGER. Nothing retrains on a timer. A strategy is retrained when one of two
   things has actually changed: enough NEW MARKET DAYS have arrived since the
   last fit, or enough NEW CLEAN LIVE TRADES have closed. Refitting the same
   parameters to the same data on a schedule is not learning, it is a way to
   eventually stumble into a lucky number.

2. FIT ON TRAIN ONLY. The panel is cut chronologically, never at random: a
   random split lets tomorrow teach the model about today, which is the single
   easiest way to build a backtest that cannot be reproduced live.

3. READ THE HOLDOUT ONCE. The challenger is evaluated on the held-out tail one
   time. Peeking, re-tuning, and re-reading turns a holdout into a training set
   with extra steps.

4. RE-RUN THE CHAMPION ON THE SAME BARS. This is the "running back on old data"
   step, and it matters because a challenger that looks good against a REMEMBERED
   champion number is usually just being compared against a different market.
   Both models are re-run over the identical window, and over the full history,
   so the comparison is like for like. Every previous champion is re-run too --
   "the old one/s", plural, because a model that beats last week's and loses to
   the one from a month ago has not improved anything.

5. REFUSE BY DEFAULT. Promotion requires the challenger to beat the incumbent by
   more than two standard errors of the difference. A better point estimate is
   not evidence; almost every "improvement" at this sample size is noise, and
   this project has the scar tissue to prove it -- an hour-of-day gate and a
   momentum-fade rule both looked significant and both flipped sign on held-out
   data.

WHAT PROMOTION DOES AND DOES NOT DO
-----------------------------------
Promotion swaps the parameters the strategy runs with IN PAPER. It cannot move
anything to live money: that still requires the statistical gates in model_lab,
which are stricter and independent. Nothing here can spend real money, by
construction.
"""
from __future__ import annotations

import json
import math
import time

from app.core import db
from app.research import backtest as bt
from app.strategy.registry import STRATEGIES, build, retrain_grid

# A challenger must clear the incumbent by this many standard errors of the
# difference. 2.0 is the ordinary two-sigma bar; at these sample sizes anything
# looser promotes noise.
PROMOTE_SIGMAS = 2.0
# Below this many held-out trades the held-out mean is not an estimate of
# anything, so no promotion regardless of how good it looks.
MIN_HOLDOUT_TRADES = 30
# Triggers. Either is enough on its own.
RETRAIN_AFTER_NEW_DAYS = 14
RETRAIN_AFTER_NEW_TRADES = 20
# Fraction of the panel kept back. Chronological, always the tail.
HOLDOUT_FRAC = 0.25
# Hard bound on the sweep so a job cannot run away.
MAX_CANDIDATES = 48


def ensure_schema() -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS model_versions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy     TEXT NOT NULL,
            fitted_ts    REAL NOT NULL,
            params_json  TEXT NOT NULL,
            train_json   TEXT,
            holdout_json TEXT,
            full_json    TEXT,
            is_champion  INTEGER NOT NULL DEFAULT 0,
            promoted     INTEGER NOT NULL DEFAULT 0,
            verdict      TEXT,
            comparison   TEXT,
            trigger      TEXT,
            n_days_data  INTEGER,
            n_live_trades INTEGER
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_modelver ON model_versions(strategy, fitted_ts)")


# ──────────────────────────────── triggers ──────────────────────────────────

def _last_fit(strategy: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM model_versions WHERE strategy=? ORDER BY fitted_ts DESC LIMIT 1",
        (strategy,))


def _data_days() -> int:
    # A full scan of the bars table, asked once PER STRATEGY by status(), which
    # the Strategies tab polls: 7 x 1.9 s under the one lock per poll
    # (2026-09-21, GET /system/threads). The count changes once a day.
    # day-boundary-ok: counts how many days of history exist before a refit is allowed; one day either way cannot change that gate
    row = db.query_one_cached("SELECT COUNT(DISTINCT date(ts,'unixepoch','localtime')) AS d FROM bars",
                              ttl_s=600.0)
    return int(row["d"] or 0) if row else 0


def _clean_trades(strategy: str) -> int:
    from app.feedback import defects
    rows = db.query("SELECT ts_close, strategy FROM trades WHERE strategy=? AND mode='paper'",
                    (strategy,))
    return sum(1 for r in rows if not defects.is_excluded(dict(r)))


def should_retrain(strategy: str) -> dict:
    """Has anything actually changed since the last fit?"""
    ensure_schema()
    last = _last_fit(strategy)
    days, trades = _data_days(), _clean_trades(strategy)
    if last is None:
        return {"strategy": strategy, "due": True, "trigger": "never fitted",
                "days": days, "trades": trades}
    new_days = days - int(last["n_days_data"] or 0)
    new_trades = trades - int(last["n_live_trades"] or 0)
    if new_days >= RETRAIN_AFTER_NEW_DAYS:
        return {"strategy": strategy, "due": True, "days": days, "trades": trades,
                "trigger": f"{new_days} new days of market data since the last fit"}
    if new_trades >= RETRAIN_AFTER_NEW_TRADES:
        return {"strategy": strategy, "due": True, "days": days, "trades": trades,
                "trigger": f"{new_trades} new clean trades closed since the last fit"}
    return {
        "strategy": strategy, "due": False, "days": days, "trades": trades,
        "trigger": "",
        "why": (f"nothing new enough: {new_days}/{RETRAIN_AFTER_NEW_DAYS} new days, "
                f"{new_trades}/{RETRAIN_AFTER_NEW_TRADES} new clean trades. Refitting "
                f"the same data on a schedule is not learning."),
        "needs_days": max(0, RETRAIN_AFTER_NEW_DAYS - new_days),
        "needs_trades": max(0, RETRAIN_AFTER_NEW_TRADES - new_trades),
    }


# ──────────────────────────────── the sweep ─────────────────────────────────

def _candidates(strategy: str) -> list[dict]:
    """Parameter sets to try, always including the incumbent unchanged."""
    g = retrain_grid(strategy)
    base = build(strategy).params
    out: list[dict] = [{}]              # {} == defaults == the incumbent
    if not g:
        return out
    keys = sorted(g)
    combos: list[dict] = [{}]
    for k in keys:
        nxt = []
        for c in combos:
            for v in g[k]:
                if len(nxt) >= MAX_CANDIDATES:
                    break
                nxt.append({**c, k: v})
        combos = nxt
    for c in combos:
        if any(base.get(k) != v for k, v in c.items()):
            out.append(c)
    return out[:MAX_CANDIDATES]


def _metrics(res: bt.BTResult) -> dict:
    nets = [t.net_ret for t in res.trades] if res.trades else []
    n = len(nets)
    mean = sum(nets) / n if n else 0.0
    if n > 1:
        var = sum((x - mean) ** 2 for x in nets) / (n - 1)
        se = math.sqrt(var / n)
    else:
        var = se = 0.0
    return {"n": n, "mean_net_pct": mean * 100.0, "se_pct": se * 100.0,
            "sd_pct": math.sqrt(var) * 100.0,
            "total_net_pct": sum(nets) * 100.0,
            "win_rate": (sum(1 for x in nets if x > 0) / n) if n else 0.0}


def _evaluate(panel, strategy: str, params: dict, cost_bps: float,
              start_i: int | None, end_i: int | None) -> dict:
    strat = build(strategy, **params)
    res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps,
                          apply_hurdle=False, start_i=start_i, end_i=end_i)
    return _metrics(res)


# ──────────────────────────────── the run ───────────────────────────────────

def run(strategy: str, panel=None, cost_bps: float | None = None,
        force: bool = False) -> dict:
    """Fit a challenger, test it, and let it displace the champion only on evidence."""
    ensure_schema()
    trig = should_retrain(strategy)
    if not trig["due"] and not force:
        return {"strategy": strategy, "ran": False, **trig}

    if panel is None:
        from app.execution import engine               # local import: heavy
        panel = engine.build_panel(refresh=False, granularity=3600, limit=35000)
    if cost_bps is None:
        from app.execution import cost_model
        cost_bps = cost_model.estimate(None).per_side_bps

    T = panel.T
    split = int(T * (1.0 - HOLDOUT_FRAC))
    if T < 200 or split <= 0:
        return {"strategy": strategy, "ran": False,
                "why": f"only {T} bars in the panel — not enough to split"}

    # 2. FIT ON TRAIN ONLY.
    scored = []
    for cand in _candidates(strategy):
        try:
            m = _evaluate(panel, strategy, cand, cost_bps, None, split)
        except Exception as exc:
            db.log_event("WARNING", "retrain", f"{strategy} candidate failed: {exc}")
            continue
        if m["n"] >= 10:
            scored.append((m["mean_net_pct"], cand, m))
    if not scored:
        return {"strategy": strategy, "ran": False,
                "why": "no candidate produced enough trades on the training window"}
    scored.sort(key=lambda x: x[0], reverse=True)
    best_train_mean, challenger_params, challenger_train = scored[0]

    # 3. READ THE HOLDOUT ONCE.
    challenger_hold = _evaluate(panel, strategy, challenger_params, cost_bps, split, None)
    challenger_full = _evaluate(panel, strategy, challenger_params, cost_bps, None, None)

    # 4. RE-RUN THE CHAMPION, AND EVERY PREVIOUS CHAMPION, ON THE SAME BARS.
    champ_row = db.query_one(
        "SELECT * FROM model_versions WHERE strategy=? AND is_champion=1 "
        "ORDER BY fitted_ts DESC LIMIT 1", (strategy,))
    champ_params = json.loads(champ_row["params_json"]) if champ_row else {}
    champ_hold = _evaluate(panel, strategy, champ_params, cost_bps, split, None)
    champ_full = _evaluate(panel, strategy, champ_params, cost_bps, None, None)

    rivals = []
    seen = {json.dumps(champ_params, sort_keys=True)}
    for r in db.query("SELECT params_json, fitted_ts FROM model_versions "
                      "WHERE strategy=? ORDER BY fitted_ts DESC LIMIT 6", (strategy,)):
        key = r["params_json"]
        if key in seen:
            continue
        seen.add(key)
        try:
            pm = json.loads(key)
        except Exception:
            continue
        rivals.append({"fitted_ts": r["fitted_ts"], "params": pm,
                       "holdout": _evaluate(panel, strategy, pm, cost_bps, split, None)})

    # 5. REFUSE BY DEFAULT.
    diff = challenger_hold["mean_net_pct"] - champ_hold["mean_net_pct"]
    se_diff = math.sqrt(challenger_hold["se_pct"] ** 2 + champ_hold["se_pct"] ** 2)
    sigmas = (diff / se_diff) if se_diff > 0 else 0.0
    beats_all = all(challenger_hold["mean_net_pct"] > rv["holdout"]["mean_net_pct"]
                    for rv in rivals) if rivals else True
    same_params = (json.dumps(challenger_params, sort_keys=True)
                   == json.dumps(champ_params, sort_keys=True))

    if same_params:
        verdict, promote = "unchanged", False
        why = "the training window picked the incumbent's own parameters — nothing to promote"
    elif challenger_hold["n"] < MIN_HOLDOUT_TRADES:
        verdict, promote = "rejected", False
        why = (f"only {challenger_hold['n']} held-out trades, need {MIN_HOLDOUT_TRADES} "
               f"before the held-out mean estimates anything")
    elif sigmas < PROMOTE_SIGMAS:
        verdict, promote = "rejected", False
        why = (f"beat the champion by {diff:+.3f}%/trade, which is {sigmas:.2f} standard "
               f"errors — under the {PROMOTE_SIGMAS:.0f}-sigma bar. A better point estimate "
               f"is not evidence at this sample size")
    elif not beats_all:
        verdict, promote = "rejected", False
        why = ("cleared the current champion but lost to an earlier version on the same "
               "bars — that is drift, not improvement")
    else:
        verdict, promote = "promoted", True
        why = (f"beat the champion by {diff:+.3f}%/trade over {challenger_hold['n']} "
               f"held-out trades, {sigmas:.2f} standard errors, and beat every earlier "
               f"version on the same bars")

    comparison = {
        "challenger": {"params": challenger_params, "train": challenger_train,
                       "holdout": challenger_hold, "full": challenger_full},
        "champion": {"params": champ_params, "holdout": champ_hold, "full": champ_full},
        "earlier_versions": rivals,
        "difference_pct_per_trade": diff, "sigmas": sigmas,
        "bar": f"{PROMOTE_SIGMAS:.0f} standard errors",
    }

    if promote:
        db.execute("UPDATE model_versions SET is_champion=0 WHERE strategy=?", (strategy,))
    db.execute(
        "INSERT INTO model_versions(strategy, fitted_ts, params_json, train_json, "
        " holdout_json, full_json, is_champion, promoted, verdict, comparison, trigger, "
        " n_days_data, n_live_trades) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (strategy, time.time(), json.dumps(challenger_params),
         json.dumps(challenger_train), json.dumps(challenger_hold),
         json.dumps(challenger_full), 1 if promote else 0, 1 if promote else 0,
         f"{verdict}: {why}", json.dumps(comparison), trig.get("trigger", ""),
         trig["days"], trig["trades"]))

    db.log_event("INFO" if promote else "INFO", "retrain",
                 f"{strategy}: {verdict} — {why}")
    return {"strategy": strategy, "ran": True, "verdict": verdict, "promoted": promote,
            "why": why, "trigger": trig.get("trigger", ""), **comparison}


def active_params(strategy: str) -> dict:
    """Parameters the engine should build this strategy with.

    Defaults unless a challenger has earned promotion. Reading this rather than
    the class defaults is what makes a promotion mean anything.
    """
    ensure_schema()
    row = db.query_one("SELECT params_json FROM model_versions "
                       "WHERE strategy=? AND is_champion=1 ORDER BY fitted_ts DESC LIMIT 1",
                       (strategy,))
    if not row:
        return {}
    try:
        return json.loads(row["params_json"])
    except Exception:
        return {}


def history(strategy: str | None = None, limit: int = 25) -> list[dict]:
    """Every fit ever attempted, promoted or refused, for the app to show."""
    ensure_schema()
    sql = ("SELECT id, strategy, fitted_ts, params_json, holdout_json, is_champion, "
           "promoted, verdict, trigger FROM model_versions ")
    args: tuple = ()
    if strategy:
        sql += "WHERE strategy=? "
        args = (strategy,)
    sql += "ORDER BY fitted_ts DESC LIMIT ?"
    rows = db.query(sql, args + (limit,))
    out = []
    for r in rows:
        d = dict(r)
        for k in ("params_json", "holdout_json"):
            try:
                d[k.replace("_json", "")] = json.loads(d.pop(k) or "{}")
            except Exception:
                d[k.replace("_json", "")] = {}
        out.append(d)
    return out


def status() -> dict:
    """What is due, what is not, and how far off the rest is."""
    # studied(), not ACTIVE: retraining is exactly what a parked strategy needs.
    from app.strategy.registry import studied
    checks = [should_retrain(s) for s in studied()]
    return {"due": [c for c in checks if c["due"]],
            "waiting": [c for c in checks if not c["due"]],
            "recent": history(limit=10),
            "rule": (f"a challenger must beat the incumbent by {PROMOTE_SIGMAS:.0f} standard "
                     f"errors on held-out bars, and beat every earlier version on the same "
                     f"bars, or it is refused")}
