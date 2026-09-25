"""Automatic research. Everything retrains itself; you keep the controls.

the operator's requirement: backtesting, model training, validation, evaluation and
reports should happen on their own, not because someone remembered to click a
button — but with the ability to pause or restart any of it.

So each job is a small named unit with an interval, its own enable flag, and a
recorded outcome. The loop runs them when due, one at a time, and never lets one
failing job stop the others. State lives in the database, so a restart resumes
exactly where it left off rather than re-running everything at once.

Deliberate design points
------------------------
  * Jobs run SEQUENTIALLY. A walk-forward is CPU-heavy and running four at once
    on a laptop that is also collecting live data helps nobody.
  * Every run records duration, status and a one-line summary, so "is the
    research current?" is answerable at a glance instead of by reading logs.
  * A failing job is retried on its normal schedule with backoff, not abandoned
    and not hammered.
  * Pausing is global and instant; disabling is per job and persists.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path

from app.core import db

CHECK_INTERVAL_S = 20.0

_state = {"thread": None, "running": False, "paused": False,
          "current": None, "started_at": None}
_lock = threading.Lock()

HOUR = 3600.0
DAY = 86400.0


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS scheduler_jobs (
        name TEXT PRIMARY KEY,
        interval_s REAL NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run REAL, last_status TEXT, last_duration_s REAL,
        last_summary TEXT, last_error TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        run_count INTEGER NOT NULL DEFAULT 0)""")


# ── the jobs ─────────────────────────────────────────────────────────────────
def _job_universe_review() -> str:
    from app.data import selection
    r = selection.review()
    return (f"{r['counts']['tradeable']} tradeable, {r['counts']['watch']} watch, "
            f"{len(r['changes'])} role changes")


def _job_prediction_markets() -> str:
    """Polymarket's crypto markets, hourly: the crowd's odds on BTC/ETH price
    levels today and this month. Context for the regime read; nothing in the
    execution path reads it (app/data/prediction_markets.py)."""
    from app.data import prediction_markets as pm
    r = pm.fetch()
    if not r.get("ok"):
        raise RuntimeError(f"polymarket refused: {r.get('error')}")
    return f"{r['stored']} crypto market(s) stored"


def _job_macro() -> str:
    from app.data import macro
    r = macro.fetch()
    errs = r.get("errors") or []
    if not r["series_loaded"] and errs:
        # Every series failing at once is the SOURCE refusing, not seven wrong
        # symbols. Stooq is a free keyless CSV endpoint and blocks periodically.
        # This stays a loud failure so it is visible, but the message says what
        # it is: nothing in the execution, strategy, risk or feedback path reads
        # macro_bars. It is screen context, and trading is unaffected.
        raise RuntimeError(
            f"all {len(errs)} macro series failed — the Stooq CSV endpoint is "
            f"refusing or unreachable. Macro is display context only; no trading "
            f"decision reads it. First error: {str(errs[0])[:160]}")
    return (f"{len(r['series_loaded'])} series, {r['rows']:,} rows, {len(errs)} errors"
            + (f" -- first: {str(errs[0])[:120]}" if errs else ""))


def _job_news() -> str:
    from app.data import news
    r = news.fetch()
    errs = r.get("errors") or []
    if not r["considered"] and errs:
        raise RuntimeError(f"no headlines and {len(errs)} feed errors -- first: {str(errs[0])[:200]}")
    return (f"{r['considered']} items, {len(errs)} feed errors"
            + (f" -- first: {str(errs[0])[:110]}" if errs else ""))


def _job_papers() -> str:
    from app.research import papers_feed
    r = papers_feed.fetch()
    return f"{r['added']} new papers from {r['sources']}"


# Research granularity. The live loop works on 1-minute bars, but only weeks of
# those exist and they cannot be backfilled. Every research job therefore runs on
# HOURLY bars, where there are four years. Running the walk-forward on minute data
# was why breakout_train reported "not enough events: 0" — it was looking at days.
RESEARCH_GRANULARITY = 3600
RESEARCH_BARS = 35000            # ~4 years of hourly


def _job_breakout_train() -> str:
    from app.execution import engine
    from app.research import breakout
    from app.core import health
    # A 35,000-bar panel is the single largest allocation this process makes.
    # Attempting it while already heavy is how the backend got SIGKILLed in the
    # middle of a 20-minute training run, taking the dashboard down with it.
    room = health.headroom(health.panel_cost_mb(RESEARCH_BARS, 80))
    if not room["ok"]:
        return f"skipped: {room['reason']}"
    panel = engine.build_panel(refresh=False, granularity=RESEARCH_GRANULARITY,
                               limit=RESEARCH_BARS)
    rep = breakout.load_or_train(panel, force=True)
    if not rep.get("available"):
        return f"not enough events: {rep.get('n_events', 0)}"
    o = rep["out_of_sample"]
    return (f"{rep['n_events']} events, AUC {o['auc']:.3f}, "
            f"{'usable' if o['beats_chance_at_95'] else 'no skill'}")


def _research_panel_for(name: str, hourly_panel):
    """The panel a strategy's research must run on: its OWN bar size.

    Every research job built one hourly panel and handed it to every strategy.
    pump_catch reads 15-minute bars; walking it forward on hourly bars would
    grade a rule the desk never runs. Same RESEARCH_BARS budget (35,000 bars is
    ~1 year at 15 minutes, which is all the venue has anyway), same headroom
    check, cached per job pass.
    """
    from app.execution import engine
    from app.strategy.registry import build
    try:
        bs = int(getattr(build(name), "bar_seconds", RESEARCH_GRANULARITY) or RESEARCH_GRANULARITY)
    except Exception:
        bs = RESEARCH_GRANULARITY
    if bs == RESEARCH_GRANULARITY:
        return hourly_panel
    cache = _research_panels.setdefault("panels", {})
    if bs not in cache:
        cache[bs] = engine.build_panel(refresh=False, granularity=bs, limit=RESEARCH_BARS)
    return cache[bs]


_research_panels: dict = {}


def _job_model_lab() -> str:
    from app.execution import engine
    from app.research import model_lab
    from app.core import health
    from app.strategy.registry import studied
    # This job ran for THREE HOURS AND EIGHT MINUTES on its last pass, doing
    # full walk-forward analysis on fast_flip, forced_momentum, regime_swing and
    # lead_lag_rotation -- all four of which are retired and can never trade.
    # Three hours of grinding inside the trading process, on answers nobody
    # would act on, during the window macOS chose to kill it. Report on what is
    # actually running; a retired strategy's verdict is already recorded.
    room = health.headroom(health.panel_cost_mb(RESEARCH_BARS, 80))
    if not room["ok"]:
        return f"skipped: {room['reason']}"
    panel = engine.build_panel(refresh=False, granularity=RESEARCH_GRANULARITY,
                               limit=RESEARCH_BARS)
    out = []
    for name in studied():
        try:
            rep = model_lab.run(name, _research_panel_for(name, panel), include_walk_forward=True)
            out.append(f"{name}={rep.get('verdict', '?')}")
        except Exception as exc:
            out.append(f"{name}=ERROR({type(exc).__name__}: {str(exc)[:90]})")
    if out and all("=ERROR(" in o for o in out):
        raise RuntimeError("every strategy failed: " + "; ".join(out))
    return "; ".join(out)


def _job_evolve() -> str:
    """Accumulate what nothing was watching, and measure a shape once it recurs.

    Deliberately slow to conclude: a shape must show up on several separate days
    and produce enough coin-days before it is measured at all, and measuring it
    records a verdict — it never ships a strategy. Building a rule the moment a
    shape appears is the failure mode this project keeps finding in itself.
    """
    from app.research import daily_report, evolve
    absorbed = 0
    for row in db.query("SELECT day FROM daily_reports ORDER BY day DESC LIMIT 30"):
        absorbed += evolve.absorb_day(row["day"]).get("absorbed", 0)
    ready = [s for s in evolve.standings() if s["ready_to_measure"]]
    for shape in ready:
        # The measurement itself is heavy and shape-specific; record that the
        # threshold was crossed so it is visible, and let the research pass pick
        # it up rather than fitting inside the trading process.
        evolve.record_verdict(
            shape["shape"], "ready to measure",
            {"coin_days": shape["coin_days"], "days_seen": shape["days_seen"],
             "avg_best_pct": round(shape["avg_best"] or 0, 3)})
    fit = [t for t in (evolve.training_set(s) for s in
                       ("day_climb", "morning_dip", "pump_ride", "volume_build"))
           if t["ready_to_fit"]]
    bits = [f"absorbed {absorbed}"]
    if ready:
        bits.append(f"{len(ready)} shape(s) crossed the threshold: "
                    + ", ".join(s["shape"] for s in ready))
    if fit:
        bits.append(f"{len(fit)} strategy(ies) now have enough live rows to fit")
    return "; ".join(bits)



def _job_retrain() -> str:
    """Fit a challenger, test it, re-run every old model on the same bars, and
    promote only on evidence.

    This is the step that was missing: `evolve` accumulated to "ready to measure"
    and then nothing measured. Now the threshold actually fires something, and
    that something is allowed to say no -- which it usually will, because at these
    sample sizes most improvements are noise.
    """
    from app.core import health
    from app.execution import engine
    from app.research import retrain
    from app.strategy.registry import studied  # noqa: F401  (retrain check below)

    due = [c for c in (retrain.should_retrain(s) for s in studied()) if c["due"]]
    if not due:
        return "nothing due — no strategy has seen enough new data since its last fit"

    room = health.headroom(health.panel_cost_mb(RESEARCH_BARS, 80))
    if not room["ok"]:
        return f"deferred: {room['why']}"
    panel = engine.build_panel(refresh=False, granularity=RESEARCH_GRANULARITY,
                               limit=RESEARCH_BARS)
    if panel is None or getattr(panel, "T", 0) < 200:
        return "deferred: panel too small to split into train and holdout"

    bits = []
    _research_panels.clear()
    for check in due:
        name = check["strategy"]
        try:
            rep = retrain.run(name, panel=_research_panel_for(name, panel))
        except Exception as exc:
            db.log_event("WARNING", "retrain", f"{name} retrain failed: {exc}")
            bits.append(f"{name}: failed ({exc})")
            continue
        if not rep.get("ran"):
            continue
        bits.append(f"{name}: {rep['verdict']}")
    # The conviction curve is cheap and depends on closed trades rather than the
    # panel, so it refreshes on the same beat.
    try:
        from app.feedback import sizing
        fitted = sizing.refit_all()
        if fitted["fitted"]:
            bits.append(f"sizing curve refit for {len(fitted['fitted'])} strategy(ies)")
    except Exception as exc:
        db.log_event("WARNING", "retrain", f"sizing refit failed: {exc}")
    return "; ".join(bits) or "ran, nothing promoted"



def _job_invariants() -> str:
    """Check the statements that must be true of the data, and shout if one is not.

    This exists because every gate this repo had checked the CODE, and the NULL
    order linkage lived in the DATA -- four days of green checks over a broken
    join. A scheduled check is the only kind that would have caught it: a test
    somebody has to remember to run was not run either.
    """
    from app.research import invariants
    out = invariants.run_all()
    if out["n_broken"]:
        for r in out["results"]:
            if r["status"] == "broken":
                db.log_event("ERROR", "invariant",
                             f"{r['name']}: {r['detail']}",
                             {"question": r["question"],
                              "would_have_caught": r["would_have_caught"]})
    return out["headline"]



def _job_spreads() -> str:
    """Read Robinhood's own bid/ask per coin and store the real spread.

    This was a button on the Venue Lab page and a permanent line on the Setup
    checklist telling the operator to go press it. He asked, reasonably, what he
    was supposed to do with that — and the honest answer is nothing, because it
    is a machine's job: it reads quotes, it needs no judgement, and it goes stale
    on its own as spreads drift. So it runs daily and the checklist reports it
    instead of asking for it.

    Cost is the largest term in every result this desk produces, and until a coin
    is measured it is priced at the published 0.95% default. Close, but not its
    own number.
    """
    # no broker integration in this build — the order-placing and account code was removed when this repository was published. Each coin keeps the published 0.95%/side default, which is what
    # every cost figure in this project already uses.
    return "no broker integration in this build — the order-placing and account code was removed when this repository was published"
    out = {}
    n = out.get("measured") or out.get("updated") or 0
    errs = out.get("errors") or []
    bits = [f"{n} coin(s) measured"]
    if errs:
        bits.append(f"{len(errs)} could not be read")
    return "; ".join(bits)



def _job_daily_lab() -> str:
    from app.research import daily_lab
    out = daily_lab.run("paper", act=True)
    bits = []
    for x in sorted(out["scored"], key=lambda z: z["t_stat"]):
        if x["n"]:
            bits.append(f"{x['strategy']} {x['mean_pct']:+.2f}%/trade (n={x['n']})")
    head = ""
    if out["demoted"]:
        head += "SWITCHED OFF: " + ", ".join(out["demoted"]) + ". "
    if out["promoted"]:
        head += "switched back on: " + ", ".join(out["promoted"]) + ". "
    b = out.get("book") or {}
    if b.get("trades"):
        head += (f"Today {b['trades']} trades, net ${b['net_usd']:+.2f}; worst was "
                 f"{b.get('worst_hour')} at ${b.get('worst_hour_usd', 0):+.2f} across "
                 f"{b.get('stops_in_worst_hour')} of them. ")
    return head + ("; ".join(bits) if bits else "nothing scored yet")


def _job_exit_lab() -> str:
    """Replay every newly closed trade against every exit rule we know of.

    Costs nothing and risks nothing: it writes rows. When one rule is far enough
    ahead to be believed, promotion goes through the same champion-versus-
    challenger bar as everything else.
    """
    from app.research import exit_lab, pending
    out = exit_lab.absorb()
    for e in pending.fire_ready():
        db.log_event("INFO", "pending",
                     f"ready to act on: {e['idea']} — {e['on_ready']}")
    if not out["replayed"]:
        return f"nothing new ({out['already_done']} trades already replayed)"
    st = exit_lab.standings()
    best = st["rules"][0] if st["rules"] else None
    bits = [f"replayed {out['replayed']} trade(s)"]
    if out["no_bars"]:
        bits.append(f"{out['no_bars']} had no bars to replay")
    if best and st["n_trades"] >= exit_lab.MIN_FOR_A_VERDICT:
        bits.append(f"best so far: {best['rule']} at {best['mean_net_pct']:+.2f}%/trade")
    elif best:
        bits.append(f"{st['n_trades']}/{exit_lab.MIN_FOR_A_VERDICT} trades toward a verdict")
    return "; ".join(bits)


def _job_rolling_entry() -> str:
    """Does volume_build's trigger fire earlier on a rolling 60-minute window,
    and does it fire on more junk? research/rolling_entry.py decides; this
    just runs it daily and records the answer where the Test Lab can show it."""
    from app.research import rolling_entry
    res = rolling_entry.compare(days=365)
    if not res.get("available"):
        return f"skipped: {res.get('why')}"
    rolling_entry.record(res)
    m = res["matched"]
    return (f"{res['span_days']:.0f}d, {res['coins']} coins: hourly {res['hourly'].get('n', 0)} entries "
            f"at {res['hourly'].get('mean_net_pct', 0):+.2f}%, rolling {res['rolling'].get('n', 0)} at "
            f"{res['rolling'].get('mean_net_pct', 0):+.2f}%; {m['n']} matched, rolling led by "
            f"{(m.get('median_lead_min') or 0):.0f} min; {res['extra_rolling_entries'].get('n', 0)} extra at "
            f"{res['extra_rolling_entries'].get('mean_net_pct', 0):+.2f}% — {res.get('verdict', '')}")


def _job_day_shape() -> str:
    """When a coin's day has turned bear, what does the rest of the day do?
    Every coin-day in the hourly history, read at 06/09/12 local. See
    research/day_shape.py."""
    from app.research import day_shape
    res = day_shape.study()
    if not res.get("available"):
        return f"skipped: {res.get('why')}"
    day_shape.record(res)
    bits = []
    for k, c in sorted(res["cells"].items()):
        b = c.get("bear", {})
        if b.get("n"):
            bits.append(f"{k}: bear n={b['n']} rest {b['mean_rest_pct']:+.2f}% "
                        f"({c.get('sigmas', 0):+.1f}σ vs not-bear)")
    return f"{res['coins']} coins x {res['days']} days — " + "; ".join(bits)


def _job_entry_lateness() -> str:
    """Does buying late (most of the move in the last hour, at the recent high)
    predict a worse outcome? Every historical entry, per strategy.
    research/entry_lateness.py."""
    from app.execution import engine
    from app.research import entry_lateness
    from app.strategy.registry import studied
    from app.core import health
    room = health.headroom(health.panel_cost_mb(RESEARCH_BARS, 80))
    if not room["ok"]:
        return f"skipped: {room['reason']}"
    panel = engine.build_panel(refresh=False, granularity=RESEARCH_GRANULARITY, limit=RESEARCH_BARS)
    _research_panels.clear()
    bits = []
    for name in studied():
        try:
            res = entry_lateness.study(name, panel=_research_panel_for(name, panel))
        except Exception as exc:
            bits.append(f"{name}: failed ({type(exc).__name__})")
            continue
        if not res.get("available"):
            bits.append(f"{name}: {res.get('why')}")
            continue
        entry_lateness.record(res)
        v = res["verdicts"]
        bits.append(f"{name}: {res['n_entries']} entries, last-hour rho "
                    f"{(v['last_hour_pct'].get('rho') or 0):+.3f} (p {v['last_hour_pct'].get('p_value')}), "
                    f"vs-high rho {(v['vs_high_pct'].get('rho') or 0):+.3f}")
    return "; ".join(bits)


def _job_entry_quality() -> str:
    """P(win after costs) at the signal bar, fitted on each strategy's own
    historical entries and read once on the last quarter. A report, never a
    trade. research/entry_quality.py."""
    from app.execution import engine
    from app.research import entry_quality
    from app.strategy.registry import studied
    from app.core import health
    room = health.headroom(health.panel_cost_mb(RESEARCH_BARS, 80))
    if not room["ok"]:
        return f"skipped: {room['reason']}"
    panel = engine.build_panel(refresh=False, granularity=RESEARCH_GRANULARITY, limit=RESEARCH_BARS)
    _research_panels.clear()
    bits = []
    for name in studied():
        try:
            res = entry_quality.fit(name, panel=_research_panel_for(name, panel))
        except Exception as exc:
            bits.append(f"{name}: failed ({type(exc).__name__}: {str(exc)[:80]})")
            continue
        entry_quality.record(res)
        if not res.get("available"):
            bits.append(f"{name}: {res.get('why')}")
            continue
        h = res["holdout"]
        tb = h.get("top_vs_bottom") or {}
        bits.append(f"{name}: {res['n_events']} entries, held-out AUC {h['auc']:.3f} "
                    f"[{h['auc_ci95'][0]:.3f}, {h['auc_ci95'][1]:.3f}], top-bottom quintile "
                    f"{tb.get('top_minus_bottom_pct', 0):+.2f}% ({tb.get('sigmas', 0):+.1f}σ) — "
                    f"{'USABLE for ranking' if res['usable_for_ranking'] else 'not yet'}")
    return "; ".join(bits)


def _job_regime_days() -> str:
    """Name every historical day's weather from the data, score every strategy
    per regime, and record today's regime for the router. research/regime_days.py."""
    from app.research import regime_days
    res = regime_days.study()
    if not res.get("available"):
        return f"skipped: {res.get('why')}"
    regime_days.record(res)
    bits = [f"k={res['k']} over {res['days']} days: " + ", ".join(f"{k} {v}" for k, v in sorted(res["counts"].items()))]
    for s_, card in res["scorecard"].items():
        for reg, c in (card.get("by_regime") or {}).items():
            if abs(c.get("sigmas", 0.0)) >= 2:
                bits.append(f"{s_} is {c['verdict']} on {reg} ({c['mean_net_pct']:+.2f}% vs "
                            f"{card['overall']['mean_net_pct']:+.2f}%, {c['sigmas']:+.1f}σ, n={c['n']})")
    if res.get("today"):
        bits.append(f"today: {res['today']['regime']} (p={res['today']['p']:.2f})")
    return "; ".join(bits)


def _job_regime_today() -> str:
    """Cheap hourly read: once READ_HOUR has passed, classify today under the
    last fitted model so the router has today's weather without a refit."""
    import datetime as _dt
    from app.research import regime_days
    from app.execution import engine
    now = _dt.datetime.now(regime_days.TZ)
    if now.hour < regime_days.READ_HOUR:
        return f"before {regime_days.READ_HOUR:02d}:00 local — today is not readable yet"
    if regime_days.today():
        return f"today already read: {regime_days.today()['regime']}"
    res = regime_days.latest()
    if not res or not res.get("model"):
        return "no fitted regime model yet — the daily regime_days job fits it"
    from app.data import selection
    keep = set(selection.tradeable_symbols()) | set(getattr(selection, "CORE", []))
    panel = engine.build_panel(refresh=False, granularity=3600, limit=24 * 8)
    rows = regime_days.day_features(panel, keep)
    if not rows or rows[-1]["date"] != now.date().isoformat():
        return "today's 09:00 bar is not in the panel yet"
    model = {"names": res["model"]["names"], "standardise": res["model"]["standardise"],
             "model": res["model"]["model"]}
    name, p = regime_days.classify(model, rows[-1])
    today = {"date": rows[-1]["date"], "regime": name, "p": p,
             **{f: rows[-1][f] for f in regime_days.FEATURES}}
    db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES ('regime_today', ?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
               (json.dumps(today), time.time()))
    return f"today is '{name}' (p={p:.2f}); breadth {today['breadth']:.2f}, median 24h {today['median_24h']:+.1f}%"


def _job_relearn() -> str:
    """Score every strategy on its own realised trades; pause or resume on evidence."""
    from app.feedback import relearn
    res = relearn.run("paper")
    if res["actions"]:
        return "; ".join(res["actions"])[:300]
    live = [f"{c['strategy']} {c.get('mean_net_pct', 0):+.2f}% n={c['n']}" for c in res["cards"]]
    return "no change — " + ", ".join(live)[:280]


def _job_daily_report() -> str:
    """Build yesterday's report (now final) and refresh today's."""
    from app.research import daily_report as _dr
    res = _dr.backfill(max_days=14)
    return f"built/refreshed {res['count']} day(s): {', '.join(res['built'][-3:]) or 'none'}"


def _job_hour_profile() -> str:
    """Learn what each hour of the day is worth to each strategy, from data.

    This is what replaces the hard-coded trading window. It replays each active
    strategy's own entry rule over the stored hourly bars, records the forward
    return by hour, and hands the samples to hour_profile.fit(), which decides by
    empirical Bayes how much of each hour's apparent edge is real. If an apparent
    difference is just noise, the weights come back at 1.000 and the clock stops
    mattering -- nobody has to notice and edit a constant.
    """
    from app.execution import engine
    from app.strategy import hour_profile
    from app.strategy.registry import studied, STRATEGIES
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("America/Chicago")
    HOLD = 14                       # hours forward, the horizon these rules trade
    panel = engine._panel_for(3600, bars=4000, refresh=False)
    if panel is None or getattr(panel, "T", 0) < 200:
        return "not enough hourly bars yet"
    T = panel.T
    done = []
    for name in studied():
        cls = STRATEGIES.get(name)
        if not cls:
            continue
        strat = cls()
        strat.params = cls.defaults()
        warm = max(strat.warmup_bars(), 2)
        samples: list[tuple[int, float]] = []
        # Every 2nd bar: enough samples, half the work. The entries are not
        # de-overlapped here on purpose -- this measures the HOUR, not a book.
        for t in range(warm, T - HOLD, 2):
            try:
                sigs = strat.generate(panel, t)
            except Exception:
                continue
            if not sigs:
                continue
            hour = datetime.fromtimestamp(float(panel.ts[t]), TZ).hour
            for sig in sigs[:3]:
                j = panel.symbols.index(sig.symbol) if sig.symbol in panel.symbols else None
                if j is None:
                    continue
                p0, p1 = panel.close[t, j], panel.close[t + HOLD, j]
                if p0 and p1 and p0 > 0:
                    samples.append((hour, (p1 / p0 - 1.0) * 100.0))
        prof = hour_profile.fit(samples)
        hour_profile.save(name, prof)
        done.append(f"{name}: {len(samples)} samples, {prof.get('verdict', '')[:60]}")
    if not done:
        return "no active strategies"
    return " | ".join(done)


def _job_posteriors() -> str:
    from app.feedback import loop
    from app.core import mode
    from app.strategy.registry import STRATEGIES
    n = 0
    failed = []
    for s in STRATEGIES:
        try:
            loop.update_posterior(s, mode.get_mode())
            n += 1
        except Exception as exc:
            failed.append(f"{s}({type(exc).__name__})")
    if n == 0 and failed:
        raise RuntimeError("every posterior update failed: " + ", ".join(failed[:5]))
    return (f"{n} strategy posteriors refreshed"
            + (f", {len(failed)} failed: {', '.join(failed[:4])}" if failed else ""))


def _job_rh_spreads() -> str:
    """Re-measure Robinhood's real spread on every coin we can trade.

    This is the number every cost decision in the app rests on: the hurdle a
    signal must clear, the round trip the exit lab charges, the cost column on
    the Daily tab. It is measured from Robinhood's own quotes -- not guessed --
    but it was only ever measured when somebody pressed a button, so all 58
    observations came from a single batch and then aged.

    A spread is not a constant. It widens when the book thins, which is exactly
    when the desk is most likely to be trading a mover. A cost model built on a
    day-old number is a cost model that is wrong at the moment it matters most.
    """
    from app.execution import rh_spread          # noqa: F401
    # no broker integration in this build — the order-placing and account code was removed when this repository was published.
    out = {"measured": 0, "note": "no broker integration in this build — the order-placing and account code was removed when this repository was published"}
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(str(out["error"])[:300])
    # `measured` is the LIST of coins; `n` is the count. Reading the list as a
    # count would have reported a truthy object as the number measured and
    # looked fine forever.
    n = int((out or {}).get("n") or 0)
    errs = (out or {}).get("errors") or []
    if n == 0:
        raise RuntimeError(
            "measured 0 coins" + (f"; first error: {str(errs[0])[:200]}" if errs else
                                  " and reported no errors — check the credentials"))
    rows = rh_spread.table()
    measured = sum(1 for r in rows if r.get("is_measured"))
    return (f"re-measured {n} coin(s); {measured}/{len(rows)} now carry a real "
            f"Robinhood spread rather than the default")


def _job_vault() -> str:
    """Make sure the vault has every order and every trade the database has.

    The synchronous write in core/vault.py is the primary path; this is the one
    that makes it checkable. It should almost always find nothing to do, and the
    day it finds something is the day it earned its place.
    """
    from app.core import vault
    v = vault.reconcile()
    if v.get("error"):
        raise RuntimeError(f"vault reconcile failed: {v['error']}")
    if v.get("failed"):
        raise RuntimeError(
            f"{v['failed']} record(s) could not be written to the vault at "
            f"{v.get('dir')} — check the folder exists and is writable")
    st = vault.status()
    return (f"{v['checked']} checked, {v['added']} appended; "
            f"vault holds {st['records']} record(s) across {st['days']} day(s) "
            f"at {st['dir']}")


def _job_vault_mirror() -> str:
    """Push the vault off this machine. Never required for correctness.

    Exit 3 means "no remote configured yet", which is a state, not a fault --
    reporting it as a failure every hour would train the operator to ignore the
    one place failures are shown. Anything else non-zero IS a fault.
    """
    import subprocess
    from app.config import get_settings
    root = Path(get_settings().db_file).parent.parent
    try:
        r = subprocess.run(["bash", str(root / "scripts" / "vault_mirror.sh"), "--quiet"],
                           capture_output=True, text=True, timeout=180)
    except Exception as exc:
        raise RuntimeError(f"could not run the mirror: {type(exc).__name__}: {exc}")
    if r.returncode == 3:
        # No remote, by the operator's choice for now. Say what the LOCAL
        # history holds, because "local vault is current" hid the fact that
        # nothing was being committed at all until 2026-09-19.
        from app.core import vault as _vault
        try:
            vdir = str(_vault.vault_dir())
            n = subprocess.run(["git", "-C", vdir, "rev-list", "--count", "HEAD"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
            last = subprocess.run(["git", "-C", vdir, "log", "-1", "--format=%cI"],
                                  capture_output=True, text=True, timeout=20).stdout.strip()
            hist = f"local git history: {n} commit(s), last {last or 'never'}"
        except Exception as exc:
            hist = f"local git history unreadable ({type(exc).__name__})"
        return (f"committed locally, pushed nowhere — {hist}; no git remote is configured "
                f"(scripts/vault_mirror.sh prints the one-time setup)")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "unknown error").strip()[:400])
    return (r.stdout or "pushed").strip().splitlines()[-1][:200]


def _job_ledger() -> str:
    """Snapshot everything that Coinbase cannot hand back to us."""
    from app.core import housekeeping, setup_ops
    # Cheap, and it keeps the id high-water mark tracking reality between boots.
    # A mark that only updates at startup goes stale the moment a trade closes,
    # and a stale mark is exactly what lets a restore reissue a used id.
    setup_ops.enforce_trade_id_high_water_mark()
    r = housekeeping.ledger_snapshot("scheduled")
    return f"{r['rows']:,} rows, {r['tables']} tables, {r['megabytes']:.1f} MB"


def _job_housekeeping() -> str:
    from app.core import housekeeping, dbrecover
    from app.config import get_settings as _gs
    b = housekeeping.backup("daily")
    # The cheap header check runs at every boot; the page-by-page walk is too
    # slow for startup, so it runs here, once a day, where nobody is waiting.
    ok, why, corrupt = dbrecover.opens_cleanly(_gs().db_file, deep=True)
    if not ok:
        db.log_event("ERROR", "housekeeping",
                     (f"INTEGRITY CHECK FAILED: {why} — restore from data/backups"
                      if corrupt else f"integrity check could not run: {why}"))
    r = housekeeping.run()
    bk = (f"backup {b['megabytes']:.0f} MB; " if b.get("ok")
          else f"BACKUP FAILED ({b.get('error')}); ")
    tail = (f"freed {r['freed_mb']:.0f} MB, now {r['after_mb']:.0f} MB, "
            f"{r['free_gb']:.1f} GB free on disk"
            + (" (vacuumed)" if r["vacuumed"] else ""))
    if not b.get("ok"):
        # RAISE, do not summarise. Until 2026-09-18 a failed backup was a string
        # inside an otherwise-successful job: status "ok", last_error None,
        # consecutive_failures 0. The backup is the single thing that made the
        # corruption survivable, and it had been failing for a day with the word
        # "ok" next to it. A job whose most important step failed did not succeed.
        raise RuntimeError(f"daily backup failed: {b.get('error')} ({tail})")
    return bk + tail


def _job_minute_topup() -> str:
    """Keep 1-minute bars current for the coins we actually trade.

    Minute data cannot be backfilled from any public venue, so the only way to
    ever have a year of it is to collect a day of it every day.
    """
    from app.data import backfill, selection
    # The [:25] cap here was invisible and expensive: DayScan prefers the finest
    # granularity available, so 25 coins having minute bars made the whole scan
    # show 25 of 75 coins. Cover everything we might trade.
    syms = selection.tradeable_symbols()
    if not syms:
        syms = [r["symbol"] for r in db.query(
            "SELECT symbol FROM universe WHERE active=1 AND rh_confirmed=1")]
    before = db.query_one("SELECT MAX(ts) t FROM bars WHERE granularity=60")
    res = backfill.start(granularity=60, days=3, symbols=syms)
    # This used to return 1.7ms after *starting* a background backfill and report
    # 'ok' regardless of what happened next, which is how minute bars sat 152
    # minutes stale with a green status beside them. Report the real age.
    age = ((time.time() - before["t"]) / 60.0) if (before and before["t"]) else None
    if not res.get("started"):
        return f"skipped: {res.get('reason')}"
    return (f"1-min top-up started for {len(syms)} coins"
            + (f"; newest minute bar was {age:,.0f} min old" if age is not None else ""))


def _job_history_topup() -> str:
    """Keep the 15-minute and hourly history CURRENT.

    This job was missing, and its absence was quiet and expensive. Hourly and
    15-minute bars were only ever written by a manual backfill, so after that
    one run the research history froze: hourly bars ended 13 hours before the
    research jobs that read them, and drifted further every day. Every
    walk-forward, every acceptance gate and every breakout model was being
    fitted on a window that stopped in the past.

    The live loop refreshes 1-minute bars as a side effect of building its
    panel, which is why only the fast path looked healthy.

    Cost is trivial: three days at hourly is 72 candles, one request per coin.
    """
    from app.data import backfill, selection

    def _txt(v):
        """Salvaged rows can come back as bytes; a bytes symbol becomes the
        literal string "b\'XRP\'" in the request URL and 404s on every call."""
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", "replace")
        return v

    syms = sorted({_txt(s) for s in selection.tracked_symbols() if s})
    feed_rows = db.query("SELECT symbol, feed_product FROM universe WHERE active=1")
    products = {_txt(r["symbol"]): _txt(r["feed_product"]) for r in feed_rows}
    done = 0
    rows_written = 0
    errors = 0
    first_error = None
    for gran, days in ((3600, 5.0), (900, 3.0)):
        for sym in syms:
            product = products.get(sym)
            if not product:
                continue
            try:
                rows_written += backfill.backfill_symbol(sym, product, gran, days)
                done += 1
            except Exception as exc:
                errors += 1
                if first_error is None:
                    first_error = f"{sym}: {type(exc).__name__}: {str(exc)[:160]}"

    # ── deepen thin history ──────────────────────────────────────────────────
    #
    # The loop above keeps the last five days current. It never reaches further
    # back, so a coin adopted last month stays fourteen days deep FOREVER -- too
    # thin to backtest, too thin to warm up, and therefore untradeable no matter
    # what Robinhood says about it.
    #
    # Measured 2026-09-11: Robinhood confirms 58 coins. 33 had four years of
    # hourly bars. The other 25 -- AERO, ASTER, AVNT, BILL, BNB, CHIP, FLOKI,
    # HYPE, JTO, MEGA and the rest -- had exactly 336 bars each, all starting
    # 2026-08-28, the day they were adopted. Every "no opportunity" result this
    # project has produced was measured on 33 coins because the other 25 had no
    # history to measure, and nothing was ever going to give them any.
    #
    # A few per run, chosen at random so every thin coin gets its turn, and the
    # feed returns whatever it actually has -- a coin genuinely listed two weeks
    # ago simply stays short, which is the truth rather than a gap.
    DEEP_TARGET_BARS = 4000          # ~166 days: clears any strategy's warm-up
    DEEP_PER_RUN = 5
    deepened = []
    try:
        thin = db.query(
            """SELECT u.symbol AS symbol, u.feed_product AS feed_product,
                      COUNT(b.ts) AS n
               FROM universe u
               LEFT JOIN bars b ON b.symbol = u.symbol AND b.granularity = 3600
               WHERE u.active = 1
               GROUP BY u.symbol, u.feed_product
               HAVING n < ?
               ORDER BY RANDOM() LIMIT ?""", (DEEP_TARGET_BARS, DEEP_PER_RUN))
        for r in thin:
            sym = _txt(r["symbol"]); product = _txt(r["feed_product"])
            if not product:
                continue
            try:
                wrote = backfill.backfill_symbol(sym, product, 3600, 400.0)
                rows_written += wrote
                done += 1
                if wrote:
                    deepened.append(f"{sym}+{wrote:,}")
            except Exception as exc:
                errors += 1
                if first_error is None:
                    first_error = f"{sym} (deepen): {type(exc).__name__}: {str(exc)[:120]}"
    except Exception as exc:
        db.log_event("WARNING", "scheduler", f"deepen step skipped: {exc}")

    # A run that wrote nothing while every call failed is a FAILURE, not an "ok".
    # It was reported as ok for eleven hours on 2026-09-07 while every symbol
    # 404'd, so the four-hour timer applied and nothing retried or alerted.
    if done == 0 or (rows_written == 0 and errors > 0):
        raise RuntimeError(
            f"history top-up wrote no rows: {errors} errors across {len(syms)} symbols"
            + (f" -- first: {first_error}" if first_error else ""))

    stale = _stalest_granularity()
    if deepened:
        db.log_event("INFO", "scheduler",
                     "deepened thin history: " + ", ".join(deepened))
    return ((f"deepened {len(deepened)} thin coin(s): {', '.join(deepened)}; " if deepened else "")
            + f"{rows_written:,} rows across {done} symbol-granularity pairs"
            + (f", {errors} errors ({first_error})" if errors else "")
            + (f"; NEWEST {stale[0]}s BAR IS {stale[1]:.1f}h OLD" if stale else ""))


def _stalest_granularity() -> tuple[int, float] | None:
    """Flag any research granularity that has quietly stopped advancing."""
    worst = None
    # 60 was missing from this list, so nothing ever noticed that minute bars had
    # stopped advancing -- 900s and 3600s were 2 minutes old while 60s was 152.
    for gran in (60, 900, 3600):
        row = db.query_one("SELECT MAX(ts) t FROM bars WHERE granularity=?", (gran,))
        if not row or not row["t"]:
            continue
        age_h = (time.time() - row["t"]) / 3600.0
        limit = {60: 1.0, 900: 3.0}.get(gran, 4.0)
        if age_h > limit and (worst is None or age_h > worst[1]):
            worst = (gran, age_h)
    return worst


JOBS: dict[str, dict] = {
    "history_topup": {"fn": _job_history_topup, "interval": 4 * HOUR,
                      "retry": 15 * 60, "stale_check": True,
                      "what": "Keep 15-minute and hourly bars current — research reads these."},
    "universe_review": {"fn": _job_universe_review, "interval": DAY,
                        "what": "Re-decide which coins are tradeable, watched or dropped."},
    "evolve": {"fn": _job_evolve, "interval": 12 * HOUR,
               "what": "Accumulate the shapes nothing was watching; measure one once it "
                       "has recurred enough to be worth measuring."},
    "retrain": {"fn": _job_retrain, "interval": 12 * HOUR,
                "what": "When enough new data has arrived, fit a challenger, test it on "
                        "held-out bars, re-run every previous model on the same bars, and "
                        "promote only if it wins by two standard errors."},
    "spreads": {"fn": _job_spreads, "interval": DAY, "retry": 30 * 60,
                "what": "Read Robinhood's own bid/ask per coin so cost is each coin's "
                        "real spread rather than the 0.95% published default."},
    "rolling_entry": {"fn": _job_rolling_entry, "interval": DAY,
                      "what": "Replay volume_build's trigger on a rolling 60-minute window "
                              "(pump_catch) against the calendar hour on the same coins and "
                              "the same exit: does it fire earlier, and on more junk?"},
    "entry_lateness": {"fn": _job_entry_lateness, "interval": DAY,
                       "what": "Every historical entry per strategy: does buying late (most of the "
                               "move in the last hour, at the recent high) predict a worse outcome?"},
    "entry_quality": {"fn": _job_entry_quality, "interval": DAY,
                      "what": "Fit P(win after costs) at the signal bar on each strategy's own "
                              "historical entries; read the last quarter once. A report, not a trade."},
    "regime_days": {"fn": _job_regime_days, "interval": DAY,
                    "what": "Name every day's weather from the data (Gaussian mixture on breadth, "
                            "returns, dispersion, volatility), replay every strategy per regime, "
                            "and record today's regime for the sizing router."},
    "regime_today": {"fn": _job_regime_today, "interval": HOUR, "retry": 20 * 60,
                     "what": "After 09:00 local, classify today under the fitted regime model so "
                             "the router has today's weather."},
    "day_shape": {"fn": _job_day_shape, "interval": DAY,
                  "what": "Every coin-day in the history, read at 06/09/12 local: when the "
                          "day has turned bear (below open, lower highs, lower lows), what "
                          "does the rest of the day do? Decides whether cutting a bear day pays."},
    "daily_lab": {"fn": _job_daily_lab, "interval": 4 * HOUR,
                  "what": "Score every strategy on its recent record, write the day's "
                          "scorecard, and switch off the ones the evidence condemns "
                          "(the operator's own switch always outranks it)."},
    "exit_lab": {"fn": _job_exit_lab, "interval": 2 * HOUR,
                 "what": "Replay each closed trade against every exit rule, so the "
                         "next exit change is chosen on evidence rather than argument."},
    "invariants": {"fn": _job_invariants, "interval": HOUR,
                   "what": "Check the things that must be true of the data — order "
                           "linkage, P&L arithmetic, spread floors, hold lengths — and "
                           "log an error the moment one stops being true."},
    "relearn": {"fn": _job_relearn, "interval": 6 * HOUR,
                "what": "Compare every strategy's real trades with what its backtest "
                        "promised; pause the ones the evidence condemns."},
    "daily_report": {"fn": _job_daily_report, "interval": 6 * HOUR,
                     "what": "Build the per-day report: what moved, what we took, "
                             "what nothing was watching."},
    "hour_profile": {"fn": _job_hour_profile, "interval": DAY,
                     "what": "Learn what each hour of day is worth to each strategy, "
                             "instead of hard-coding a trading window."},
    "posteriors": {"fn": _job_posteriors, "interval": HOUR,
                   "what": "Update each strategy's edge belief from its recent trades."},
    "news": {"fn": _job_news, "interval": HOUR, "retry": 20 * 60,
             "what": "Pull headlines and flag coins in a hack, delisting or regulatory event."},
    # Hourly, not 6-hourly: the point of this job is freshness, and at 6 hours it
    # guaranteed up to 6 hours of stale minute data by construction.
    "minute_topup": {"fn": _job_minute_topup, "interval": HOUR, "retry": 20 * 60,
                     "stale_check": True,
                     "what": "Collect 1-minute bars for traded coins — the only way to ever have them."},
    "macro": {"fn": _job_macro, "interval": DAY,
              "what": "Refresh equities, dollar, gold, oil and VIX daily bars."},
    "prediction_markets": {"fn": _job_prediction_markets, "interval": HOUR,
                           "what": "Polymarket's odds on BTC/ETH price levels — crowd context for the regime read."},
    "breakout_train": {"fn": _job_breakout_train, "interval": DAY,
                       "what": "Retrain the false-breakout veto and re-check that it still beats chance."},
    "model_lab": {"fn": _job_model_lab, "interval": DAY,
                  "what": "Full walk-forward and acceptance gates for every strategy."},
    # Ten minutes, not a day: the daily full backup is 450 MB and mostly bars,
    # which refetch themselves. This one is a few MB of trades, orders, signals
    # and models — the rows that are gone forever if the file dies.
    "ledger": {"fn": _job_ledger, "interval": 10 * 60, "retry": 5 * 60,
               "what": "Snapshot every table that cannot be refetched from an exchange."},
    # Same cadence as the ledger snapshot, and for the same reason: these are
    # the rows that are gone forever if something goes wrong, and the vault is
    # the copy that is not in this folder, not in a database, and not read by
    # anything here.
    # Hourly. Robinhood rate limits, and 75 quotes an hour is nothing, but the
    # spread genuinely moves intraday and a stale one silently mis-prices every
    # entry decision.
    "rh_spreads": {"fn": _job_rh_spreads, "interval": 60 * 60, "retry": 15 * 60,
                   "what": "Re-measure Robinhood's real per-coin spread, the number "
                           "every cost decision rests on."},
    "vault": {"fn": _job_vault, "interval": 10 * 60, "retry": 5 * 60,
              "what": "Append to the write-once vault anything it does not already have."},
    "vault_mirror": {"fn": _job_vault_mirror, "interval": 60 * 60, "retry": 15 * 60,
                     "what": "Push the vault to its private off-machine remote."},
    "housekeeping": {"fn": _job_housekeeping, "interval": DAY,
                     "what": "Prune old minute bars and quotes, checkpoint the WAL, compact the database."},
    "papers": {"fn": _job_papers, "interval": 7 * DAY,
               "what": "Check for new quantitative-finance papers worth reading."},
}


def _seed() -> None:
    """Create missing job rows, and carry code-side interval changes across.

    INSERT OR IGNORE alone never updates an existing row, so changing an
    interval in JOBS silently did nothing to any database that already had the
    job. minute_topup was the proof: the code was changed from six-hourly to
    hourly, the comment explaining why is still there, and the desk went on
    collecting minute bars every six hours for weeks. A fix that cannot reach
    production is not a fix.

    An interval a human set through the API is still theirs. We can tell the
    difference because code_default_s remembers what the code last asked for:
    a row still sitting on that value was never touched by hand.
    """
    ensure_schema()
    try:
        db.execute("ALTER TABLE scheduler_jobs ADD COLUMN code_default_s REAL")
    except Exception:
        pass                                      # already there

    for name, spec in JOBS.items():
        want = float(spec["interval"])
        db.execute("INSERT OR IGNORE INTO scheduler_jobs"
                   "(name, interval_s, enabled, code_default_s) VALUES (?,?,1,?)",
                   (name, want, want))
        row = db.query_one("SELECT interval_s, code_default_s FROM scheduler_jobs "
                           "WHERE name=?", (name,))
        if row is None:
            continue
        have = float(row["interval_s"])
        was = row["code_default_s"]
        if was is None:                           # legacy row: adopt what it has
            db.execute("UPDATE scheduler_jobs SET code_default_s=? WHERE name=?",
                       (have, name))
            was = have
        if abs(want - float(was)) < 1.0:
            continue                              # code has not changed its mind
        if abs(have - float(was)) < 1.0:
            db.execute("UPDATE scheduler_jobs SET interval_s=?, code_default_s=? "
                       "WHERE name=?", (want, want, name))
            db.log_event("INFO", "scheduler",
                         f"{name} interval {have/3600:.1f}h -> {want/3600:.1f}h "
                         f"(code default changed)")
        else:
            db.execute("UPDATE scheduler_jobs SET code_default_s=? WHERE name=?",
                       (want, name))
            db.log_event("WARN", "scheduler",
                         f"{name} is set to {have/3600:.1f}h by hand; the code now "
                         f"asks for {want/3600:.1f}h and was left alone")


def due(name: str) -> bool:
    """Is this job actually overdue, or would running it now be duplicate work?

    Boot used to run universe_review, hour_profile and daily_report every single
    time, unconditionally — jobs the scheduler already owns on daily and 6-hourly
    cadences. On a restart to pick up a code change, that is minutes of API calls
    and computation redoing what was done minutes earlier, and it is most of what
    "the app takes forever to load" actually was.
    """
    try:
        ensure_schema()
        row = db.query_one("SELECT * FROM scheduler_jobs WHERE name=?", (name,))
        return True if row is None else _due(dict(row))
    except Exception:
        return True          # cannot tell => do the work, never skip silently


def _due(row: dict) -> bool:
    if not row["enabled"]:
        return False
    if row["last_run"] is None:
        return True

    name = row["name"]
    spec = JOBS.get(name, {})
    interval = row["interval_s"]
    fails = row["consecutive_failures"] or 0

    if fails:
        # Back off a job that keeps failing -- but never let it vanish. The old
        # rule multiplied by up to 8x, so a failing 4-hour data job would not be
        # retried for 32 hours. A job that declares `retry` uses that instead,
        # so a stalled collector comes back in minutes rather than a day.
        retry = spec.get("retry")
        if retry:
            interval = min(retry * min(2 ** (fails - 1), 4), interval)
        else:
            interval *= min(2 ** fails, 8)
    elif spec.get("stale_check") and _data_is_stale():
        # Self-heal: if the bars this job is responsible for have stopped
        # advancing, it is due regardless of when it last claimed success.
        return True

    return (time.time() - row["last_run"]) >= interval


def _data_is_stale() -> bool:
    """Have the research granularities stopped advancing?

    On 2026-09-07 the 15-minute and hourly collectors stopped for eleven hours
    while the job kept reporting success, because every fetch failed and the
    failures were counted rather than raised. Freshness is now checked against
    the bars themselves, not against the job's own opinion of itself.
    """
    try:
        for gran, limit_h in ((900, 3.0), (3600, 4.0)):
            row = db.query_one("SELECT MAX(ts) t FROM bars WHERE granularity=?", (gran,))
            if row and row["t"] and (time.time() - row["t"]) / 3600.0 > limit_h:
                return True
    except Exception:
        return False
    return False


# One job at a time, across every caller.
#
# The loop, the "run now" button and the boot catch-up are three separate
# threads that all call run_job. Nothing stopped two of them overlapping, and
# two concurrent history_topup runs means every coin fetched twice and a good
# chance of being rate-limited by the feed. The loop's own "one job per pass"
# comment was only ever true of the loop.
_run_lock = threading.Lock()


def run_job(name: str) -> dict:
    """Run one job now, regardless of schedule. Serialised against all callers."""
    _seed()
    spec = JOBS.get(name)
    if not spec:
        raise KeyError(f"unknown job {name!r}")
    with _run_lock:
        return _run_job_locked(name, spec)


def _run_job_locked(name: str, spec: dict) -> dict:
    t0 = time.time()
    with _lock:
        _state["current"] = name
    try:
        summary = spec["fn"]()
        db.execute(
            """UPDATE scheduler_jobs SET last_run=?, last_status='ok', last_duration_s=?,
               last_summary=?, last_error=NULL, consecutive_failures=0,
               run_count=run_count+1 WHERE name=?""",
            (time.time(), time.time() - t0, str(summary)[:400], name))
        db.log_event("INFO", "scheduler", f"{name}: {summary}")
        out = {"job": name, "status": "ok", "summary": summary,
               "duration_s": time.time() - t0}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        db.execute(
            """UPDATE scheduler_jobs SET last_run=?, last_status='error', last_duration_s=?,
               last_error=?, consecutive_failures=consecutive_failures+1,
               run_count=run_count+1 WHERE name=?""",
            (time.time(), time.time() - t0, err[:400], name))
        db.log_event("ERROR", "scheduler", f"{name} failed: {err}",
                     {"traceback": traceback.format_exc()[-1500:]})
        out = {"job": name, "status": "error", "error": err,
               "duration_s": time.time() - t0}
    finally:
        with _lock:
            _state["current"] = None
    return out


def _loop() -> None:
    _seed()
    while _state["running"]:
        try:
            if not _state["paused"]:
                rows = db.query("SELECT * FROM scheduler_jobs ORDER BY last_run IS NOT NULL, last_run")
                for r in rows:
                    if not _state["running"] or _state["paused"]:
                        break
                    if _due(r):
                        run_job(r["name"])
                        break            # one job per pass; never pile them up
        except Exception as exc:
            db.log_event("ERROR", "scheduler", f"loop error: {exc}")
        time.sleep(CHECK_INTERVAL_S)


def start() -> dict:
    with _lock:
        if _state["running"]:
            return status()
        _seed()
        _state["running"] = True
        _state["paused"] = False
        _state["started_at"] = time.time()
        t = threading.Thread(target=_loop, daemon=True, name="tc-scheduler")
        _state["thread"] = t
        t.start()
    db.log_event("INFO", "scheduler", "automatic research started")
    return status()


def stop() -> dict:
    _state["running"] = False
    db.log_event("INFO", "scheduler", "automatic research stopped")
    return status()


def pause(flag: bool = True) -> dict:
    _state["paused"] = bool(flag)
    db.log_event("INFO", "scheduler", "paused" if flag else "resumed")
    return status()


def set_enabled(name: str, enabled: bool) -> dict:
    _seed()
    db.execute("UPDATE scheduler_jobs SET enabled=? WHERE name=?", (1 if enabled else 0, name))
    return status()


def set_interval(name: str, seconds: float) -> dict:
    _seed()
    db.execute("UPDATE scheduler_jobs SET interval_s=? WHERE name=?", (max(60.0, seconds), name))
    return status()


def status() -> dict:
    _seed()
    rows = db.query("SELECT * FROM scheduler_jobs")
    now = time.time()
    jobs = []
    for r in rows:
        spec = JOBS.get(r["name"], {})
        nxt = (r["last_run"] + r["interval_s"]) if r["last_run"] else now
        jobs.append({
            **r,
            "what": spec.get("what", ""),
            "next_run": nxt,
            "due_in_s": max(0.0, nxt - now),
            "stale": bool(r["last_run"] and (now - r["last_run"]) > r["interval_s"] * 3),
        })
    jobs.sort(key=lambda j: j["next_run"])
    return {
        "running": _state["running"],
        "paused": _state["paused"],
        "current_job": _state["current"],
        "started_at": _state["started_at"],
        "jobs": jobs,
        "note": ("Jobs run one at a time so a heavy walk-forward never competes with live "
                 "data collection. A job that fails backs off rather than retrying in a loop."),
    }
