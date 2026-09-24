"""HTTP surface. Everything the dashboard shows comes from here, and everything
here comes from the database or a named model -- there is no third source.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
from fastapi import APIRouter, Body, HTTPException, Query

from app.config import get_settings
from app.core import db, registry
from app.core import health as health_mod
from app.core import housekeeping
from app.core import mode as mode_mod
from app.core import scheduler
from app.data import attention as attention_mod
from app.data import backfill
from app.data import macro as macro_mod
from app.data import selection
from app.data import news as news_mod
from app.data import regime as regime_mod
from app.data import universe
from app.execution import budget, cost_model, desk, engine, rh_spread, symbol_cost
from app.execution.broker import get_broker
from app.feedback import loop as feedback
from app.research import backtest as bt
from app.research import breakout as breakout_mod
from app.research import ledger
from app.research import library
from app.research import papers_feed
from app.research import model_lab
from app.research import stats as S
from app.research.seasonality import calendar_effects
from app.risk import guards
from app.strategy.registry import OPERATOR_STRATEGIES, STRATEGIES, build as build_strategy, grid

router = APIRouter(prefix="/api/v1")


def ok(data, message: str = "ok"):
    return {"success": True, "data": data, "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat()}


def _clean(obj):
    """JSON cannot carry NaN/Inf; converting them to null is the honest mapping."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if not np.isfinite(f) else f
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    return obj


# ── system ────────────────────────────────────────────────────────────────────
@router.get("/liveness")
def liveness_report():
    """Which mechanisms have actually run, and which have only been built.

    The question nothing else here was asking. See core/liveness.py.
    """
    from app.core import liveness
    return ok(liveness.report())


@router.get("/vault")
def vault_status():
    """Where the write-once copy is, and how much is in it. Read-only.

    It reports the vault; it does not open it. Nothing in the app reads the
    records -- that is the point of the thing.
    """
    from app.core import vault
    return ok(vault.status())


@router.get("/system/status")
def system_status():
    s = get_settings()
    # Two of these are full scans of the largest tables in the file, and this
    # route is polled by every open tab. Shared for two minutes (db.py note).
    counts = {
        t: (db.query_one_cached if t in ("bars", "quotes") else db.query_one)(
            f"SELECT COUNT(*) c FROM {t}")["c"]
        for t in ("bars", "quotes", "signals", "orders", "trades", "cost_observations")
    }
    from app.core import code_version as _cv
    from app.core import uptime as _up
    return ok(_clean({
        "service": "tradecrypto",
        # Whether the desk is set up to survive, and whether anything is
        # deliberately holding it down. Both were invisible before: a desk that
        # only lives as long as Control Deck looked identical to one that runs
        # 24/7, and a deliberate pause looked identical to a fault.
        "uptime": _up.status(),
        # What the desk intends to do about new code on disk, so the operator
        # sees "it will apply this itself in a moment" rather than a warning
        # that looks like a chore.
        "auto_apply": __import__("app.core.autoapply", fromlist=["check"]).check(),
        # Surfaced here because a stale process is invisible otherwise: this app
        # ran a 2026-09-12 build for four days while three sessions of changes
        # sat on disk, and every screen looked fine.
        "code": _cv.status(),
        "engine": engine.status(),
        "data_counts": counts,
        "database": str(s.db_file),
        "universe_size": db.query_one("SELECT COUNT(*) c FROM universe")["c"],
        "rh_confirmed_symbols": db.query_one("SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"],
    }))


@router.get("/system/safety")
def safety():
    return ok(_clean({**get_settings().safety_report(), "broker": get_broker().probe()}))


@router.get("/decision-tree")
def decision_tree(mode: str | None = None, strategy: str | None = None):
    """The whole decision, per strategy, opened all the way up."""
    from app.core import mode as mode_mod
    from app.research import decision_tree as dt
    m = mode or mode_mod.get_mode()
    names = [strategy] if strategy else None
    return ok(_clean(dt.live(m, names)))


# ── first run: demo or live ──────────────────────────────────────────────────
#
# A correct empty dashboard is indistinguishable from a broken one, and that is
# what a new person actually meets. These four give them a way in.


@router.get("/first_run")
def first_run_state():
    from app.core import first_run
    return ok(_clean(first_run.state()))


@router.post("/first_run/demo")
def first_run_demo(payload: dict = Body(default={})):
    from app.core import first_run
    days = int(payload.get("days") or first_run.DEMO_DAYS)
    res = first_run.seed_demo(days=days)
    return ok(_clean(res), "demo book ready" if res.get("ok") else res.get("error", "could not build the demo"))


@router.post("/first_run/live")
def first_run_live():
    from app.core import first_run
    return ok(_clean(first_run.choose_live()), "starting with an empty book")


@router.post("/first_run/clear_demo")
def first_run_clear_demo():
    from app.core import first_run
    return ok(_clean(first_run.clear_demo()), "demo trades removed")


@router.post("/engine/start")
def engine_start(payload: dict = Body(default={})):
    return ok(_clean(engine.start(payload.get("strategies"))))


@router.post("/engine/stop")
def engine_stop():
    return ok(_clean(engine.stop()))


@router.post("/engine/tick")
def engine_tick(payload: dict = Body(default={})):
    """Run exactly one iteration. Useful for watching a single decision end to end."""
    return ok(_clean(engine.tick(payload.get("strategies"))))


# ── universe / market ─────────────────────────────────────────────────────────
@router.get("/universe")
def get_universe():
    return ok(_clean(universe.active_universe()))


@router.post("/universe/refresh")
def refresh_universe():
    return ok(_clean(universe.refresh_universe()))


@router.get("/movers")
def movers(limit: int = Query(25, ge=1, le=100)):
    rows = universe.movers(limit)
    obs, coef = symbol_cost.observables(), symbol_cost.fit_coefficients()
    for r in rows:
        try:
            c = symbol_cost.estimate_symbol(r["symbol"], obs, coef)
            r["est_round_trip_bps"] = c.round_trip_bps
            r["est_cost_status"] = c.status
            r["move_vs_cost"] = (abs(r["change_24h_pct"]) * 100 / c.round_trip_bps
                                 if c.round_trip_bps else None)
        except Exception:
            r["est_round_trip_bps"] = None
    return ok(_clean({
        "rows": rows,
        "caveat": ("Ranked by absolute 24h move. The widest movers usually carry the "
                   "widest spreads, so read the quoted_spread_bps column next to the "
                   "move -- a 9% mover with a 90 bps spread is not a 9% opportunity."),
        "cost_hurdle_bps": cost_model.hurdle_bps(),
    }))


@router.post("/housekeeping/backup")
def housekeeping_backup(payload: dict = Body(default={})):
    """Snapshot the database. Safe while the app is running."""
    from app.core import housekeeping
    return ok(_clean(housekeeping.backup(str(payload.get("reason", "manual")))))


@router.get("/housekeeping/backups")
def housekeeping_backups():
    from app.core import housekeeping
    return ok(_clean(housekeeping.list_backups()))


# ── guided setup: everything the dashboard can do for you ─────────────────────
@router.get("/setup/steps")
def setup_steps():
    from app.core import setup_ops
    return ok(_clean(setup_ops.status()))


@router.post("/setup/install")
def setup_install(payload: dict = Body(default={})):
    from app.core import setup_ops
    return ok(_clean(setup_ops.install(str(payload.get("package", "")))))


@router.post("/setup/keypair")
def setup_keypair():
    from app.core import setup_ops
    return ok(_clean(setup_ops.generate_keypair()))


@router.post("/setup/api-key")
def setup_api_key(payload: dict = Body(default={})):
    from app.core import setup_ops
    return ok(_clean(setup_ops.save_api_key(str(payload.get("api_key", "")))))


@router.get("/experiment")
def experiment_get():
    """Paper-only switch: take trades that do not clear the cost hurdle."""
    from app.config import get_settings
    from app.core import mode as mode_mod
    s = get_settings()
    return ok({
        "enabled": bool(getattr(s, "paper_ignore_hurdle", False)),
        "mode": mode_mod.get_mode(),
        "live_enabled": s.live_enabled,
        "what": ("Every backtest says these trades lose. This takes them anyway, "
                 "in paper only, so the prediction is measured instead of argued "
                 "about. It cannot affect live trading."),
    })


@router.post("/experiment")
def experiment_set(payload: dict = Body(default={})):
    """Flip the switch by writing it to .env, then restart to apply."""
    import re as _re
    from pathlib import Path as _P
    from app.config import PROJECT_ROOT
    on = bool(payload.get("enabled"))
    env = _P(PROJECT_ROOT) / ".env"
    text = env.read_text() if env.exists() else ""
    line = f"TC_PAPER_IGNORE_HURDLE={'true' if on else 'false'}"
    if _re.search(r"^TC_PAPER_IGNORE_HURDLE=.*$", text, _re.M):
        text = _re.sub(r"^TC_PAPER_IGNORE_HURDLE=.*$", line, text, flags=_re.M)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    env.write_text(text)
    db.log_event("WARN", "system",
                 f"paper experiment mode {'ENABLED' if on else 'disabled'}")
    return ok({"enabled": on, "restart_required": True,
               "note": "Saved. Press restart on the Setup page to apply."})


@router.get("/setup/recovered-db")
def setup_recovered_db():
    """Is there a recovered database waiting to be swapped in?"""
    from app.config import get_settings
    import sqlite3
    cand = get_settings().db_file.with_name("tradecrypto.recovered.sqlite")
    if not cand.exists():
        return ok({"available": False})
    try:
        c = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
        return ok({"available": True,
                   "integrity": c.execute("PRAGMA integrity_check").fetchone()[0],
                   "bars": c.execute("SELECT COUNT(*) FROM bars").fetchone()[0],
                   "megabytes": cand.stat().st_size / 1e6})
    except Exception as exc:
        return ok({"available": False, "error": str(exc)})


@router.post("/setup/fix-text")
def setup_fix_text():
    """Repair TEXT columns stored as BLOBs by the salvage."""
    from app.core import setup_ops
    return ok(_clean(setup_ops.fix_text_encoding()))


@router.post("/setup/restore-db")
def setup_restore_db():
    from app.core import setup_ops
    return ok(_clean(setup_ops.restore_database()))


@router.post("/paper/reset")
def paper_reset(payload: dict = Body(default={})):
    """Clear the paper trading record. Keeps every bar of price history."""
    # setup_ops was referenced here without being imported, so every press of
    # "clear paper book" raised NameError and returned a 500. The button looked
    # dead because it WAS dead -- reported twice as "I tried clearing the orders
    # but it didn't work".
    from app.core import mode as mode_mod
    from app.core import setup_ops
    m = payload.get("mode") or mode_mod.get_mode()
    if m == "mcp":
        return ok({"error": "refusing to clear a live book"})
    return ok(_clean(setup_ops.reset_paper_book(m)))


@router.post("/positions/close-all")
def positions_close_all(payload: dict = Body(default={})):
    """Flatten the book now. Paper only."""
    from app.core import mode as mode_mod
    m = payload.get("mode") or mode_mod.get_mode()
    if m == "mcp":
        return ok({"error": "refusing to flatten a live book from here"})
    return ok(_clean({"closed": engine.close_all(m, payload.get("reason", "manual"))}))


@router.get("/risk/kill-switch")
def kill_switch_status():
    """Is the kill switch engaged, when did it trip, and why."""
    from app.config import get_settings as _gs
    f = _gs().kill_switch_file
    if not f.exists():
        return ok({"engaged": False})
    try:
        lines = f.read_text().splitlines()
        return ok({"engaged": True, "tripped_ts": float(lines[0].strip()),
                   "reason": lines[1].strip() if len(lines) > 1 else "unknown"})
    except Exception as exc:
        return ok({"engaged": True, "reason": f"unreadable: {exc}"})


@router.post("/setup/restart")
def setup_restart():
    from app.core import setup_ops
    return ok(_clean(setup_ops.restart()))


# ── Robinhood REST API ────────────────────────────────────────────────────────
@router.get("/robinhood/probe")
def rh_probe():
    """Can we reach Robinhood, and are credentials in place? Places no orders."""
    from app.execution import rh_api
    return ok(_clean(rh_api.RobinhoodCrypto().probe()))


@router.post("/robinhood/sync-universe")
def rh_sync_universe():
    """Ask Robinhood which of our coins the API may actually trade.

    Returns the full list picture too. The Universe page's four tiles read
    `pairs_returned_by_robinhood`, `api_tradable_on_robinhood`, `added_count` and
    `tradable_but_feed_cannot_price` -- names this endpoint never produced, so
    they rendered as two dashes and two zeros no matter how often the button was
    pressed. The counts come from adopt_from_robinhood(); this now runs both and
    merges them, so one press answers the question it appears to ask.
    """
    from app.data import universe as _u
    from app.execution import rh_api
    try:
        synced = rh_api.sync_universe()
    except rh_api.NotConfigured as exc:
        return ok({"error": str(exc)})
    try:
        counts = _u.adopt_from_robinhood()
    except Exception as exc:                      # sync still succeeded; say so
        counts = {"counts_error": f"{type(exc).__name__}: {exc}"}
    return ok(_clean({**synced, **counts}))


@router.post("/ui-error")
def ui_error(payload: dict = Body(default={})):
    """Record a frontend crash so it is visible in the log, not only in a console.

    The Journal blank-page bug was invisible server-side: the crash happened in
    the browser during render and the backend never heard about it. Now it lands
    in events with the component stack, so the next one is diagnosable from the
    Journal's own events tab.
    """
    db.log_event("ERROR", "ui",
                 f"page crashed: {str(payload.get('page') or '?')}: "
                 f"{str(payload.get('message'))[:200]}",
                 {"stack": str(payload.get("stack") or "")[:4000]})
    return ok({"recorded": True})


@router.get("/datasources")
def datasources_list():
    """Every external API we call, plus the local tables they feed."""
    from app.research import datasources as _ds
    return ok(_clean({"sources": _ds.catalog(), "local": _ds.local()}))


@router.get("/datasources/sample")
def datasources_sample(key: str, product: str | None = None, limit: int = 25):
    """Fetch one source LIVE and return its raw rows, nothing renamed."""
    from app.research import datasources as _ds
    return ok(_clean(_ds.sample(key, product, limit)))


@router.get("/datasources/peek")
def datasources_peek(table: str, limit: int = 50):
    """The newest rows of a local table, exactly as stored."""
    from app.research import datasources as _ds
    return ok(_clean(_ds.peek(table, limit)))


@router.post("/datasources/query")
def datasources_query(payload: dict = Body(default={})):
    """Read-only SELECT against the local database."""
    from app.research import datasources as _ds
    return ok(_clean(_ds.query(payload.get("sql", ""), int(payload.get("limit", 200)))))


@router.get("/evolve")
def evolve_status():
    """What is accumulating, and how far it is from being enough to act on."""
    from app.research import evolve as _e
    return ok(_clean(_e.status()))


@router.get("/access")
def access_status():
    """How this app is reachable from a phone right now.

    It used to answer "off" whenever TC_LAN was unset — including while a
    Cloudflare tunnel was live and the operator was looking at the page ON his
    phone, through that tunnel. A status card that contradicts what the person
    can see in front of them is worse than no card.
    """
    import os
    import re
    import socket
    from app.core import access as _a

    port = os.environ.get("FRONTEND_PORT", "5180")
    lan, tun = _a.lan_enabled(), _a.tunnel_enabled()

    ip = None
    if lan:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            probe.close()
        except Exception:
            pass

    # cloudflared prints the address it was given into logs/tunnel.log.
    tunnel_url = None
    if tun:
        try:
            from pathlib import Path as _P
            # parents[4], not [3]. This file is backend/app/api/v1/routes.py, so
            # [3] is `backend/` and the log lives at the REPO root. The card
            # silently showed nothing because the file it looked for was never
            # going to exist — an off-by-one in a path, reported as "off".
            log = _P(__file__).resolve().parents[4] / "logs" / "tunnel.log"
            if log.exists():
                found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com",
                                   log.read_text(errors="ignore"))
                tunnel_url = found[-1] if found else None
        except Exception:
            pass

    token = _a.token() if (lan or tun) else None
    if tun and tunnel_url:
        url, how = f"{tunnel_url}/?token={token}", "cloudflare tunnel"
    elif lan and ip:
        url, how = f"http://{ip}:{port}/?token={token}", "same wifi"
    else:
        url, how = None, ("tunnel starting — check logs/tunnel.log" if tun
                          else "off")

    return ok(_clean({
        "enabled": bool(lan or tun),
        "how": how,
        "lan_enabled": lan,
        "tunnel_enabled": tun,
        "lan_ip": ip,
        "frontend_port": port,
        "url": url,
        "token": token,
        "works_away_from_home": bool(tun),
        "how_to_enable": ("Set TC_TUNNEL=1 in .env and restart — it works from "
                          "anywhere, including cell data, and needs no VPN on "
                          "the phone."),
        "why_off_by_default": ("This process holds your Robinhood keys and can "
                               "place orders. Reachable from the network is a "
                               "decision, not a default."),
        "install": ("On the phone, open the link once, then Share → Add to Home "
                    "Screen. It then behaves like an app and remembers the token."),
        "note": ("The address changes every time the tunnel restarts. Restarting "
                 "just the app keeps it, because the tunnel is a separate process."
                 if tun else ""),
    }))


@router.post("/access/phone")
def access_phone_toggle(payload: dict = Body(default={})):
    """Turn phone access on or off without opening a terminal or Control Deck.

    Writes TC_TUNNEL into .env, which run.sh reads at launch. It takes effect on
    the next restart — this process cannot open a tunnel for itself, because the
    tunnel is a separate program that has to be started alongside the web server.
    """
    from pathlib import Path as _P
    import re as _re
    want = bool(payload.get("enabled", True))
    env = _P(__file__).resolve().parents[3] / ".env"
    if not env.exists():
        return ok({"changed": False, "error": "no .env file found"})
    text = env.read_text()
    line = f"TC_TUNNEL={'1' if want else '0'}"
    if _re.search(r"^TC_TUNNEL=.*$", text, _re.M):
        text = _re.sub(r"^TC_TUNNEL=.*$", line, text, count=1, flags=_re.M)
    else:
        text = text.rstrip() + f"\n{line}\n"
    env.write_text(text)
    db.log_event("INFO", "setup",
                 f"phone access set to {'on' if want else 'off'}; takes effect on restart")
    return ok({"changed": True, "enabled": want,
               "next": "Restart the app for this to take effect.",
               "note": ("The tunnel keeps its address across app restarts, so an "
                        "icon already on your phone keeps working." if want else
                        "Any link already on your phone stops working.")})


@router.get("/exit-lab")
def exit_lab_standings(start: str | None = None, end: str | None = None,
                       strategy: str | None = None):
    """What every other exit rule would have returned on the trades we took."""
    from app.research import exit_lab as _el
    return ok(_clean(_el.standings(start, end, strategy)))


@router.get("/pending")
def pending_experiments():
    """Ideas deferred for want of data, and how far each is from waking up."""
    from app.research import pending as _p
    return ok(_clean(_p.status()))


@router.get("/invariants")
def invariants_status():
    """What must be true of the data, and whether it currently is."""
    from app.research import invariants as _inv
    return ok(_clean(_inv.run_all()))


@router.get("/retrain")
def retrain_status():
    """Champion vs challenger: what is due, what was refused, and why."""
    from app.research import retrain as _r
    return ok(_clean(_r.status()))


@router.get("/retrain/history")
def retrain_history(strategy: str | None = None, limit: int = 25):
    from app.research import retrain as _r
    return ok(_clean(_r.history(strategy, limit)))


@router.post("/retrain/run")
def retrain_run(payload: dict = Body(default={})):
    """Force a retrain now. Still refuses to promote unless the evidence clears the bar."""
    from app.research import retrain as _r
    name = (payload or {}).get("strategy")
    if not name:
        return ok({"error": "pass {\"strategy\": \"day_climb\"}"})
    return ok(_clean(_r.run(name, force=bool((payload or {}).get("force", True)))))


@router.get("/sizing")
def sizing_status(strategy: str | None = None, mode: str = "paper"):
    """How big the next position would be, and the arithmetic behind it."""
    from app.feedback import loop as _loop, sizing as _sz
    from app.strategy.registry import ACTIVE_STRATEGIES
    names = [strategy] if strategy else ACTIVE_STRATEGIES
    rows = []
    for n in names:
        try:
            expl = _loop.sizing_explanation(n, mode)
            expl["curve"] = _sz.curve(n)
            expl["signals_per_day"] = _sz.signals_per_day(n)
            rows.append(expl)
        except Exception as exc:
            rows.append({"strategy": n, "error": str(exc)})
    from app.risk import guards as _g
    probe = "BTC"
    try:
        floor, src = _g.min_notional_for(probe), _g.floor_source(probe)
    except Exception:
        floor, src = 0.0, "unavailable"
    return ok(_clean({
        "rows": rows,
        "floor_usd": floor,
        "floor_source": src,
        "rule": ("There is no slot count. Size is a share of free cash; how many "
                 "positions exist is whatever the cash and the venue's minimum "
                 "order size allow. The day stops when the next share would land "
                 f"under that minimum (currently ${floor:.2f}, source: {src})."),
    }))


@router.get("/strategies/scorecard")
def strategies_scorecard():
    """What each strategy's REAL trades say, against what its backtest promised."""
    from app.feedback import relearn
    return ok(_clean(relearn.scorecard()))


@router.get("/routing")
def routing_picture(mode: str = "paper"):
    """What a round trip costs under each routing, and which tier our volume earns."""
    from app.execution import routing as _r
    return ok(_clean(_r.picture(mode)))


@router.get("/routing/estimate")
def routing_estimate(symbol: str = "BTC-USD", notional_usd: float = 240.0):
    """Ask Robinhood what an order this size would really cost. Places nothing."""
    from app.execution import routing as _r
    return ok(_clean(_r.estimated_cost_live(symbol, notional_usd)))


@router.get("/report/days")
def report_days(limit: int = 400):
    """Which days the arrows can reach, newest first."""
    from app.research import daily_report as _dr
    return ok({"days": _dr.available_days(limit)})


@router.get("/report/day")
def report_day(date: str | None = None, force: bool = False):
    """One day's full report, built once and then stored."""
    from app.research import daily_report as _dr
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = date or datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    return ok(_clean(_dr.get(day, force=force)))


@router.get("/report/range")
def report_range(start: str, end: str):
    """One row per day across a range, for tables and charts."""
    from app.research import daily_report as _dr
    return ok(_clean(_dr.series(start, end)))


@router.post("/report/backfill")
def report_backfill(payload: dict = Body(default={})):
    """Build every missing day's report."""
    from app.research import daily_report as _dr
    return ok(_clean(_dr.backfill(int(payload.get("max_days", 60)))))


@router.get("/schema")
def schema_tree():
    """The database as a tree: every table, its columns, indexes, row count, and
    which tables point at it. Read from PRAGMA on the live schema, so it can
    never drift from the code the way a hand-drawn diagram does.

    Links are inferred from column NAMES (`trade_id` -> trades, `signal_id` ->
    signals ...) because this schema declares no foreign keys; the inference
    is labelled as such so nobody mistakes it for an enforced constraint.
    """
    from app.core import db as _db
    tables = [r["name"] for r in _db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    names = set(tables)
    out = []
    links = []
    for t in tables:
        cols = _db.query(f"PRAGMA table_info('{t}')")
        idx = _db.query(f"PRAGMA index_list('{t}')")
        indexes = []
        for i in idx:
            icols = [c["name"] for c in _db.query(f"PRAGMA index_info('{i['name']}')")]
            indexes.append({"name": i["name"], "unique": bool(i["unique"]), "columns": icols})
        try:
            n = _db.query_one(f"SELECT COUNT(*) AS c FROM '{t}'")["c"]
        except Exception:
            n = None
        columns = []
        for c in cols:
            ref = None
            nm = c["name"]
            if nm.endswith("_id") and nm != "id":
                base = nm[:-3]
                last = base.split("_")[-1]          # open_order_id -> order -> orders
                for cand in (base + "s", base + "es", base, last + "s", last + "es", last):
                    if cand in names and cand != t:
                        ref = cand
                        break
            if ref:
                links.append({"from": t, "column": nm, "to": ref, "inferred": True})
            columns.append({"name": nm, "type": c["type"] or "", "pk": bool(c["pk"]),
                            "notnull": bool(c["notnull"]), "default": c["dflt_value"],
                            "ref": ref})
        out.append({"table": t, "rows": n, "columns": columns, "indexes": indexes})
    return ok({"tables": out, "links": links,
               "note": ("Read live from PRAGMA table_info / index_list. Arrows are inferred "
                        "from column names (<table>_id); SQLite is not enforcing them.")})


@router.get("/architecture")
def architecture_tree(force: bool = False):
    """The whole app as a tree — page → route → module → table, and job →
    module → table — read from the source. research/architecture.py."""
    from app.research import architecture as _arch
    return ok(_clean(_arch.tree(force=force)))


_COVERAGE_MEMO: dict = {}


@router.get("/coverage/today")
def coverage_today(day: str | None = None):
    """What moved enough to be worth taking, and whether anything could see it."""
    from app.research import coverage as _cov
    # A GROUP BY over the day's bars plus two queries per coin, polled by the
    # Overview from every tab. One copy per minute is plenty (db.py note).
    key = f"coverage:{day or 'today'}"
    now = time.time()
    hit = _COVERAGE_MEMO.get(key)
    if hit and (now - hit[0]) < 60.0:
        return ok(_clean(hit[1]))
    out = _cov.today(day)
    _COVERAGE_MEMO[key] = (now, out)
    return ok(_clean(out))


@router.get("/robinhood/list-counts")
def rh_list_counts():
    """The last recorded Robinhood list check, readable without pressing anything."""
    from app.data import universe as _u
    return ok(_clean(_u.robinhood_list_counts()))


@router.get("/dayscan")
def dayscan(day: str | None = None, tradable_only: bool = True, as_of_hour: int | None = None,
            hot_volume: float | None = None, hot_swing: float | None = None):
    """One day's scan table. `as_of_hour` replays the day as it looked at that hour."""
    from app.research import dayscan as _ds
    return ok(_clean(_ds.scan_day(day, tradable_only=tradable_only, as_of_hour=as_of_hour,
                                  hot_volume=hot_volume, hot_swing=hot_swing)))


@router.get("/dayscan/range")
def dayscan_range(start: str, end: str, tradable_only: bool = True, hot_volume: float | None = None):
    """One summary line per day, for stepping back through history."""
    from app.research import dayscan as _ds
    return ok(_clean(_ds.scan_range(start, end, tradable_only=tradable_only, hot_volume=hot_volume)))


@router.get("/dayscan/days")
def dayscan_days(limit: int = 900):
    """Which days we hold bars for, newest first."""
    from app.research import dayscan as _ds
    return ok({"days": _ds.available_days(limit)})


@router.post("/robinhood/adopt-universe")
def rh_adopt_universe():
    """Add every Robinhood-API-tradable coin we are not already watching."""
    from app.data import universe as _u
    from app.execution import rh_api
    try:
        return ok(_clean(_u.adopt_from_robinhood()))
    except rh_api.NotConfigured as exc:
        return ok({"error": str(exc)})


@router.post("/robinhood/measure-spreads")
def rh_measure_spreads(payload: dict = Body(default={})):
    """Read Robinhood's own bid/ask per coin: spread measured, not assumed."""
    from app.execution import rh_api
    try:
        return ok(_clean(rh_api.measure_spreads(payload.get("symbols"))))
    except rh_api.NotConfigured as exc:
        return ok({"error": str(exc)})


@router.get("/robinhood/quote")
def rh_quote(symbol: str = "BTC-USD", quantity: str = "1"):
    """Robinhood's best bid/ask and the estimated fill for a given size."""
    from app.execution import rh_api
    c = rh_api.RobinhoodCrypto()
    try:
        return ok(_clean({"best_bid_ask": c.best_bid_ask(symbol),
                          "estimated": c.estimated_price(symbol, "both", quantity)}))
    except Exception as exc:
        return ok({"error": f"{type(exc).__name__}: {exc}"})


@router.get("/robinhood/routing-cost")
def rh_routing_cost():
    """Market-maker vs exchange routing, measured from live quotes. No order placed."""
    from app.execution import rh_api
    try:
        return ok(_clean(rh_api.routing_comparison()))
    except Exception as exc:
        return ok({"error": f"{type(exc).__name__}: {exc}"})


@router.get("/robinhood/fee-status")
def rh_fee_status():
    """Your real 30-day volume and fee tier, read from the account."""
    from app.execution import rh_api
    try:
        return ok(_clean(rh_api.fee_status()))
    except Exception as exc:
        return ok({"error": f"{type(exc).__name__}: {exc}"})


@router.get("/robinhood/fee-tiers")
def rh_fee_tiers(volume_usd: float = 0.0):
    """Robinhood's published exchange-routing fee schedule, and where you sit."""
    from app.execution import rh_api
    taker = rh_api.fee_for_volume(volume_usd, "taker")
    maker = rh_api.fee_for_volume(volume_usd, "maker")
    return ok(_clean({
        "tiers": rh_api.FEE_TIERS,
        "your_volume_usd": volume_usd,
        "taker_pct": taker, "maker_pct": maker,
        "round_trip_taker_bps": taker * 200, "round_trip_maker_bps": maker * 200,
        "caveat": ("Orders placed through v2 of the API are charged the TAKER rate "
                   "until Robinhood finishes rolling out maker/taker. v1 API orders "
                   "use market-maker routing with the spread and do not count "
                   "toward tier volume at all."),
        "source": "cdn.robinhood.com/assets/robinhood/legal/rhc-fee-schedule.pdf, read 2026-09-07",
    }))


# ── venue cost + price basis ──────────────────────────────────────────────────
@router.get("/venues")
def venues_list():
    """What each venue costs, and whether the measured edge survives it."""
    from app.execution import venue_cost
    return ok(_clean({"venues": venue_cost.venues(),
                      "measured": venue_cost.MEASURED}))


@router.post("/venues")
def venues_update(payload: dict = Body(default={})):
    from app.execution import venue_cost
    key = str(payload.pop("key", "")).strip()
    if not key:
        return ok({"error": "key is required"})
    return ok(_clean(venue_cost.set_venue(key, **payload)))


@router.get("/venues/compare")
def venues_compare(gross_edge_bps: float | None = None,
                   trades_per_day: float = 2.0, equity_usd: float = 500.0):
    from app.execution import venue_cost
    return ok(_clean(venue_cost.compare(gross_edge_bps, trades_per_day, equity_usd)))


@router.get("/price-basis")
def price_basis_report(days: float = 90.0):
    """Does our Coinbase price match Robinhood's ticket?"""
    from app.data import price_basis
    return ok(_clean(price_basis.report(days)))


@router.post("/price-basis")
def price_basis_record(payload: dict = Body(default={})):
    from app.data import price_basis
    try:
        mid = float(payload.get("rh_mid"))
    except (TypeError, ValueError):
        return ok({"error": "rh_mid must be a number read off the order ticket"})
    return ok(_clean(price_basis.record(
        str(payload.get("symbol", "")).strip().upper(), mid,
        payload.get("at"), payload.get("note"))))


# ── token ledger ──────────────────────────────────────────────────────────────
@router.get("/tokens")
def tokens_ledger():
    """Which features spend model tokens — declared, then checked against source."""
    from app.core import tokens
    return ok(_clean(tokens.ledger()))


@router.post("/tokens/provider")
def tokens_set_provider(payload: dict = Body(default={})):
    from app.core import tokens
    return ok(_clean(tokens.set_provider(
        str(payload.get("feature", "")).strip(),
        str(payload.get("provider", "")).strip())))


# ── test lab ──────────────────────────────────────────────────────────────────
@router.get("/tests")
def tests_catalogue():
    """Every test in the project, grouped by the feature it covers."""
    from app.research import testlab
    return ok(_clean(testlab.catalogue()))


@router.post("/tests/run")
def tests_run(payload: dict = Body(default={})):
    """Run the suite, one file, or one test. Empty target runs everything."""
    from app.research import testlab
    return ok(_clean(testlab.run(str(payload.get("target", "")).strip())))


@router.post("/tests/verify")
def tests_verify(payload: dict = Body(default={})):
    """Run the independent cross-check — different method, no shared code."""
    from app.research import testlab
    return ok(_clean(testlab.run_independent(str(payload.get("check", "")).strip())))


@router.get("/tests/history")
def tests_history(limit: int = 40):
    from app.research import testlab
    return ok(_clean(testlab.history(limit)))


@router.get("/tests/output")
def tests_output(run_id: int):
    from app.research import testlab
    return ok(_clean(testlab.output(run_id)))


@router.get("/tests/user")
def tests_user_read(filename: str = ""):
    from app.research import testlab
    if not filename:
        return ok({"template": testlab.TEMPLATE})
    return ok(_clean(testlab.read_user_test(filename)))


@router.post("/tests/user")
def tests_user_save(payload: dict = Body(default={})):
    from app.research import testlab
    return ok(_clean(testlab.save_user_test(
        str(payload.get("filename", "")).strip(), payload.get("source", ""))))


# ── Robinhood's published spread ──────────────────────────────────────────────
@router.get("/cost/rh-spreads")
def rh_spreads():
    """Every coin's Robinhood spread, and whether it was actually verified.

    Robinhood states the spread on the order ticket. That makes the round trip a
    known quantity rather than something to infer, which matters because cost is
    the largest single term in whether any of this is profitable.
    """
    rows = rh_spread.table()
    verified = [r for r in rows if r["source"] == "observed"]
    return ok(_clean({
        "rows": rows,
        "default_pct": rh_spread.DEFAULT_SPREAD_PCT,
        "n_verified": len(verified),
        "n_total": len(rows),
        "slippage_allowance_bps": rh_spread.SLIPPAGE_ALLOWANCE_BPS,
        "required_margin_bps": rh_spread.REQUIRED_MARGIN_BPS,
        "how_to_read_it": (
            "Open a coin on Robinhood, press Buy, and the ticket shows 'Buy spread "
            "(X%)'. Type that X here. Until you do, the coin uses the "
            f"{rh_spread.DEFAULT_SPREAD_PCT}% default and is flagged unverified."),
    }))


@router.post("/cost/rh-spreads")
def rh_spreads_set(payload: dict = Body(default={})):
    """Record the spread read off a real order ticket."""
    sym = str(payload.get("symbol", "")).strip().upper()
    if not sym:
        return ok({"error": "symbol is required"})
    try:
        pct = float(payload.get("spread_pct"))
    except (TypeError, ValueError):
        return ok({"error": "spread_pct must be a number, e.g. 0.95"})
    try:
        return ok(_clean(rh_spread.set_spread(sym, pct, note=payload.get("note"))))
    except ValueError as exc:
        return ok({"error": str(exc)})


@router.get("/cost/hurdle")
def cost_hurdle(symbol: str = "BTC"):
    """Every term in the number a signal must beat, itemised."""
    return ok(_clean(rh_spread.hurdle_breakdown(symbol)))


# ── cost lab ──────────────────────────────────────────────────────────────────
@router.get("/cost")
def cost(symbol: str | None = None):
    return ok(_clean(cost_model.estimate(symbol).to_dict()))


@router.get("/cost/breakdown")
def cost_breakdown(symbol: str | None = None, days: float = 30.0):
    return ok(_clean(cost_model.breakdown(symbol, days)))


@router.post("/cost/observation")
def add_cost_observation(payload: dict = Body(...)):
    """Log a REAL fill -- including one you placed by hand in the Robinhood app.

    Manual fills are the fastest way to replace the prior with measurement.
    Send: symbol, side, notional_usd, fill_px, mid_at_submit (the displayed price
    at the moment you tapped), and optionally mid_at_decision.
    """
    required = {"symbol", "side", "notional_usd", "fill_px", "mid_at_submit"}
    missing = required - set(payload)
    if missing:
        raise HTTPException(400, f"missing fields: {sorted(missing)}")
    res = cost_model.record_observation(
        symbol=payload["symbol"], side=payload["side"],
        notional_usd=float(payload["notional_usd"]), fill_px=float(payload["fill_px"]),
        mid_at_submit=float(payload["mid_at_submit"]),
        mid_at_decision=payload.get("mid_at_decision"),
        quoted_spread_bps=payload.get("quoted_spread_bps"),
        latency_ms=payload.get("latency_ms"),
        mode=payload.get("mode", "manual"), source="measured",
    )
    return ok(_clean({**res, "new_estimate": cost_model.estimate(payload["symbol"]).to_dict()}),
              "observation recorded; the cost model now leans on measurement rather than the prior")


# ── transparency ──────────────────────────────────────────────────────────────
@router.get("/models")
def models():
    return ok(_clean(registry.all_cards()))


@router.get("/models/{name}")
def model(name: str):
    c = registry.get(name)
    if not c:
        raise HTTPException(404, f"no model card named {name!r}")
    return ok(_clean(c.to_dict()))


@router.get("/strategies")
def strategies():
    return ok(_clean([build_strategy(n).describe() for n in STRATEGIES]))


@router.get("/allocations")
def allocations(mode: str = "paper"):
    return ok(_clean(feedback.allocations(mode)))


@router.post("/allocations/refresh")
def refresh_allocations(payload: dict = Body(default={})):
    mode = payload.get("mode", "paper")
    return ok(_clean([feedback.update_posterior(n, mode) for n in STRATEGIES]))


# ── journal ───────────────────────────────────────────────────────────────────
@router.get("/account/balance")
def account_balance(mode: str | None = None):
    """Equity, cash, what is deployed, and P&L -- what is actually left."""
    from app.core import mode as mode_mod
    from app.risk import guards
    m = mode or mode_mod.get_mode()
    acct = guards.account(m)
    acct["mode"] = m
    pos = guards.open_positions(m)
    acct["open_positions"] = len(pos)

    # The tiles used to be dead ends: "3 open" with no way to see which three,
    # and "banked / on paper" with no explanation of what either word meant. The
    # numbers behind them are cheap to compute, so they travel with the summary.
    from app.data.prices import live_price
    rows = []
    for p in pos:
        sym = p["symbol"]
        # The live quote, not the last bar close. A bar closes on a schedule, so
        # reading it showed a 40-minute-old ARB price next to a 3-second-old one
        # sitting unread in `quotes`.
        quote = live_price(sym)
        px = quote.px
        qty = float(p["qty"] or 0.0)
        entry = float(p["avg_px"] or 0.0)
        cost = qty * entry                      # buy-side spread is already in avg_px
        side = _per_side_pct(sym) / 100.0
        # What a sale would actually put back in the account, not the mid.
        exit_value = (qty * px * (1.0 - side)) if px else None
        rows.append({
            "symbol": sym,
            "strategy": p["strategy"],
            "qty": qty,
            "entry_px": entry,
            "last_px": px,
            "price_age_s": quote.age_s,
            "price_source": quote.source,
            "price_stale": quote.is_stale,
            "cost_usd": cost,
            "value_usd": exit_value,
            "pnl_usd": (exit_value - cost) if exit_value is not None else None,
            "pnl_pct": ((exit_value / cost - 1.0) * 100.0)
                       if (exit_value is not None and cost > 0) else None,
            # WHAT ACTUALLY ENDS THIS TRADE, in words.
            #
            # "target" was the wrong question to put on screen. A trailing-stop
            # strategy has no target, so the column was either a nonsense number
            # (the $2.47 ARB target) or blank — and blank explains nothing
            # either. The exit is a mechanism, so the mechanism is what shows.
            "exit_rule": _exit_rule(p),
            "stop_px": (float(p["stop_px"]) if p["stop_px"] else None),
            "target_px": (float(p["target_px"]) if p["target_px"] else None),
            "peak_px": (float(p["peak_px"]) if p["peak_px"] else None),
            "trail_pct": (float(p["trail_bps"]) / 100.0 if p["trail_bps"] else None),
            "opened_ts": p["opened_ts"],
            "hours_held": ((time.time() - float(p["opened_ts"])) / 3600.0)
                          if p["opened_ts"] else None,
        })
    rows.sort(key=lambda r: (r["pnl_usd"] is None, -(r["pnl_usd"] or 0)))
    acct["positions"] = rows
    acct["pnl_explained"] = {
        "realised_usd": acct.get("realised_pnl"),
        "unrealised_usd": acct.get("unrealised_pnl"),
        "realised_means": "closed trades, all costs paid, final",
        "unrealised_means": "open trades, priced as if sold now, all costs taken out",
        "total_usd": (acct.get("realised_pnl") or 0) + (acct.get("unrealised_pnl") or 0),
    }
    return ok(_clean(acct))



def _exit_rule(p) -> str:
    """One line saying how this position ends, and where that sits right now."""
    trail = float(p["trail_bps"] or 0.0) / 100.0
    stop = float(p["stop_px"] or 0.0)
    target = float(p["target_px"] or 0.0)
    entry = float(p["avg_px"] or 0.0)
    bits = []
    if trail > 0:
        bits.append(f"trails {trail:.0f}% below the peak"
                    + (f" — now ${stop:.5g}" if stop > 0 else ""))
    elif stop > 0:
        drop = (1 - stop / entry) * 100 if entry > 0 else 0
        bits.append(f"stop ${stop:.5g} ({drop:.1f}% below entry)")
    if target > 0:
        up = (target / entry - 1) * 100 if entry > 0 else 0
        bits.append(f"target ${target:.5g} (+{up:.1f}%)")
    else:
        bits.append("no fixed target — it runs until the trail is hit")
    return "; ".join(bits)


def _per_side_pct(symbol: str) -> float:
    """The venue's charge on ONE side, as a percent. The buy side is already
    baked into a position's avg_px, so only the sell side is still to come."""
    try:
        from app.execution import rh_spread
        return float(rh_spread.get(symbol)["spread_pct"])
    except Exception:
        return 0.95


@router.get("/chart/{symbol}")
def chart(symbol: str, day: str | None = None, granularity: int = 3600, bars: int = 400):
    """OHLCV for one coin, for one day, for the Journal's chart popup."""
    from datetime import date as _d, datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _Z
    tz = _Z("America/Chicago")
    sym = symbol.upper()
    if day:
        d0 = _d.fromisoformat(day)
        start = _dt(d0.year, d0.month, d0.day, tzinfo=tz)
        t0, t1 = int(start.timestamp()), int((start + _td(days=1)).timestamp())
        rows = db.query(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? "
            "AND granularity=? AND ts>=? AND ts<? ORDER BY ts", (sym, granularity, t0, t1))
        if len(rows) < 5 and granularity != 3600:
            rows = db.query(
                "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? "
                "AND granularity=3600 AND ts>=? AND ts<? ORDER BY ts", (sym, t0, t1))
            granularity = 3600
    else:
        rows = db.query(
            "SELECT ts, open, high, low, close, volume FROM bars WHERE symbol=? "
            "AND granularity=? ORDER BY ts DESC LIMIT ?", (sym, granularity, bars))
        rows = list(reversed(rows))
    out = [{"t": _dt.fromtimestamp(r["ts"], tz).strftime("%H:%M"), "ts": r["ts"],
            "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
            "v": r["volume"] or 0.0} for r in rows if r["close"] is not None]
    sp = db.query_one("SELECT spread_pct FROM rh_spreads WHERE symbol=?", (sym,))
    # The chart's headline price came from the last BAR close, which on an hourly
    # chart is up to an hour old — 40 minutes on the day this was noticed, while
    # a 3-second-old quote sat unread. The live quote travels with the bars.
    from app.data.prices import live_price
    lp = live_price(sym)
    return ok(_clean({"symbol": sym, "day": day, "granularity_s": granularity,
                      "bars": out, "n": len(out),
                      "live_px": lp.px, "live_age_s": lp.age_s,
                      "live_source": lp.source, "live_stale": lp.is_stale,
                      "spread_pct": sp["spread_pct"] if sp else None}))


@router.get("/signals")
def signals(limit: int = 200, decision: str | None = None, strategy: str | None = None,
            symbol: str | None = None, start_ts: float | None = None,
            end_ts: float | None = None):
    sql, params = "SELECT * FROM signals WHERE 1=1", []
    if decision:
        # The engine writes "taken_experiment" for a signal taken in paper below
        # the cost hurdle. Filtering on decision="taken" therefore matched NOTHING
        # -- 4,229 signals existed and the Journal was blank. "taken" now means
        # "an order was attempted", whichever kind.
        if decision == "taken":
            sql += " AND decision IN ('taken','taken_experiment')"
        else:
            sql += " AND decision=?"; params.append(decision)
    if strategy:
        sql += " AND strategy=?"; params.append(strategy)
    if symbol:
        sql += " AND symbol=?"; params.append(symbol.upper())
    if start_ts:
        sql += " AND ts >= ?"; params.append(start_ts)
    if end_ts:
        sql += " AND ts <= ?"; params.append(end_ts)
    return ok(_clean(db.query(sql + " ORDER BY ts DESC LIMIT ?", [*params, limit])))


@router.get("/orders")
def orders(limit: int = 200, status: str | None = None, strategy: str | None = None,
           symbol: str | None = None, start_ts: float | None = None,
           end_ts: float | None = None):
    sql, params = "SELECT * FROM orders WHERE 1=1", []
    if status:
        sql += " AND status=?"; params.append(status)
    if strategy:
        sql += " AND strategy=?"; params.append(strategy)
    if symbol:
        sql += " AND symbol=?"; params.append(symbol.upper())
    if start_ts:
        sql += " AND ts_decided >= ?"; params.append(start_ts)
    if end_ts:
        sql += " AND ts_decided <= ?"; params.append(end_ts)
    return ok(_clean(db.query(sql + " ORDER BY ts_decided DESC LIMIT ?", [*params, limit])))


@router.post("/orders/{client_id}/fill")
def report_fill(client_id: str, payload: dict = Body(...)):
    """Report a real, human-executed fill. This is the measurement, not paperwork."""
    try:
        return ok(_clean(desk.report_fill(
            client_id, float(payload["fill_price"]),
            qty=payload.get("qty"), fee_usd=float(payload.get("fee_usd", 0.0)))),
            "fill recorded; the cost model now has one more real observation")
    except KeyError:
        raise HTTPException(404, "unknown client_id")
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/trades")
def trades(limit: int = 200, mode: str = "paper", strategy: str | None = None,
           symbol: str | None = None, start_ts: float | None = None,
           end_ts: float | None = None):
    sql, params = "SELECT * FROM trades WHERE mode=?", [mode]
    if strategy:
        sql += " AND strategy=?"; params.append(strategy)
    if symbol:
        sql += " AND symbol=?"; params.append(symbol.upper())
    if start_ts:
        sql += " AND ts_close >= ?"; params.append(start_ts)
    if end_ts:
        sql += " AND ts_close <= ?"; params.append(end_ts)
    from app.feedback import defects as _defs
    rows = [dict(r) for r in db.query(sql + " ORDER BY ts_close DESC LIMIT ?",
                                      [*params, limit])]
    # Tag, never filter. A trade our own bug produced stays in the ledger and in
    # P&L — the money moved — but it is excluded from strategy scoring, and the
    # operator should be able to see which rows those are and why.
    for r in rows:
        found = _defs.defects_for(r)
        r["excluded_from_learning"] = bool(found)
        r["defects"] = [d["key"] for d in found]
        r["defect_note"] = found[0]["what"] if found else None
    return ok(_clean(rows))


@router.get("/performance")
def performance(mode: str = "paper"):
    rows = db.query("SELECT net_pnl_usd, qty, entry_px FROM trades WHERE mode=? ORDER BY ts_close", (mode,))
    rets = [r["net_pnl_usd"] / abs(r["qty"] * r["entry_px"]) for r in rows if r["qty"] and r["entry_px"]]
    if not rets:
        return ok({"n": 0, "sufficient": False,
                   "note": "no closed trades yet -- nothing can be concluded"})
    n_trials = db.query_one("SELECT COUNT(*) c FROM runs WHERE kind IN ('backtest','walkforward')")["c"] or 1
    return ok(_clean(S.performance_summary(rets, periods_per_year=365 * 24, n_trials=max(n_trials, 1))))


@router.get("/equity")
def equity(mode: str = "paper", limit: int = 2000):
    return ok(_clean(db.query(
        "SELECT * FROM equity_curve WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit))[::-1]))


@router.get("/events")
def events(limit: int = 200, level: str | None = None):
    sql = "SELECT * FROM events"
    params: list = []
    if level:
        sql += " WHERE level=?"
        params.append(level.upper())
    return ok(_clean(db.query(sql + " ORDER BY ts DESC LIMIT ?", [*params, limit])))


# ── risk ──────────────────────────────────────────────────────────────────────
@router.get("/risk/status")
def risk_status(mode: str = "paper"):
    return ok(_clean(guards.status(mode)))


@router.post("/risk/kill")
def risk_kill(payload: dict = Body(default={})):
    guards.engage_kill_switch(payload.get("reason", "engaged from dashboard"))
    return ok(_clean(guards.status(payload.get("mode", "paper"))), "kill switch engaged")


@router.post("/risk/release")
def risk_release(payload: dict = Body(default={})):
    guards.release_kill_switch()
    return ok(_clean(guards.status(payload.get("mode", "paper"))), "kill switch released")


# ── research ──────────────────────────────────────────────────────────────────
@router.post("/research/backtest")
def run_backtest(payload: dict = Body(default={})):
    name = payload.get("strategy", "top_mover_reversal")
    gran = int(payload.get("granularity", 60))
    strat = build_strategy(name, **payload.get("params", {}))
    panel = engine.build_panel(granularity=gran, limit=int(payload.get("limit", 300)),
                               refresh=bool(payload.get("refresh", True)))
    if panel.T < strat.warmup_bars() + 30:
        return ok({"insufficient_data": True, "bars_available": panel.T,
                   "bars_required": strat.warmup_bars() + 30,
                   "what_to_do": ("Leave the engine running -- it stores every candle it "
                                  "fetches, so history accumulates. Public feeds only "
                                  "serve a few hundred recent candles at a time.")})
    # Calibrating on the first half and then backtesting from bar 0 reports the
    # fitted half as if it were a result. Trade only the half the coefficients
    # never saw, with a gap so the last fitted bar cannot leak into the first
    # traded one.
    _split = panel.T // 2
    _embargo = max(strat.warmup_bars(), 30)
    strat.calibrate(panel, _split)
    cost_bps = float(payload.get("cost_bps_per_side", cost_model.estimate().per_side_bps))
    res = bt.run_backtest(panel, strat, cost_bps_per_side=cost_bps,
                          long_only=bool(payload.get("long_only", True)),
                          start_i=min(_split + _embargo, panel.T - 2))
    ppy = 365 * 24 * 3600 / max(panel.bar_seconds(), 1)
    n_trials = db.query_one("SELECT COUNT(*) c FROM runs")["c"] + 1
    summary = res.summary(ppy, n_trials=n_trials)
    out = {"strategy": strat.describe(), "cost_bps_per_side": cost_bps,
           "bars": panel.T, "symbols": panel.N, "summary": summary,
           "trades": [t.__dict__ for t in res.trades[-200:]]}
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "backtest", name, str(payload), str(summary)[:20000],
                "positive" if summary.get("positive_mean_at_95") else "not significant"))
    return ok(_clean(out))


@router.post("/research/cost-sensitivity")
def cost_sensitivity(payload: dict = Body(default={})):
    """The decisive chart: net edge as a function of execution cost."""
    name = payload.get("strategy", "top_mover_reversal")
    strat = build_strategy(name, **payload.get("params", {}))
    panel = engine.build_panel(granularity=int(payload.get("granularity", 60)),
                               limit=int(payload.get("limit", 300)), refresh=False)
    if panel.T < strat.warmup_bars() + 30:
        return ok({"insufficient_data": True, "bars_available": panel.T})
    # Calibrating on the first half and then backtesting from bar 0 reports the
    # fitted half as if it were a result. Trade only the half the coefficients
    # never saw, with a gap so the last fitted bar cannot leak into the first
    # traded one.
    _split = panel.T // 2
    _embargo = max(strat.warmup_bars(), 30)
    strat.calibrate(panel, _split)
    # `grid` shadowed the `grid` imported from strategy.registry. Harmless here
    # today, but that is exactly the shape of the bug that once disabled the
    # memory watchdog, so it gets a name of its own.
    cost_grid = payload.get("costs_bps", [0, 10, 20, 40, 60, 80, 100, 120])
    curve = bt.cost_sensitivity(panel, strat, [float(c) for c in cost_grid],
                                start_i=min(_split + _embargo, panel.T - 2))
    est = cost_model.estimate()
    return ok(_clean({
        "curve": curve,
        "measured_cost_bps_per_side": est.per_side_bps,
        "measured_source": est.source,
        "how_to_read": ("Find the row nearest your measured cost. If mean_net_bps is "
                        "negative there, the strategy does not work at this venue no "
                        "matter how good the signal looks at zero cost."),
    }))


@router.post("/research/walkforward")
def walkforward(payload: dict = Body(default={})):
    name = payload.get("strategy", "top_mover_reversal")
    cls = STRATEGIES[name]
    panel = engine.build_panel(granularity=int(payload.get("granularity", 60)),
                               limit=int(payload.get("limit", 300)), refresh=False)
    probe = cls()
    if panel.T < probe.warmup_bars() * 3:
        return ok({"insufficient_data": True, "bars_available": panel.T,
                   "bars_required": probe.warmup_bars() * 3})
    param_grid = payload.get("param_grid") or {"z_enter": [1.5, 2.0, 2.5], "hold_bars": [15, 30, 60]}
    cost_bps = float(payload.get("cost_bps_per_side", cost_model.estimate().per_side_bps))
    res = bt.walk_forward(panel, cls, param_grid, cost_bps_per_side=cost_bps,
                          n_folds=int(payload.get("n_folds", 5)))
    db.execute("INSERT INTO runs(ts, kind, label, params_json, result_json, verdict) VALUES (?,?,?,?,?,?)",
               (time.time(), "walkforward", name, str(param_grid), str(res)[:20000], res["verdict"]))
    return ok(_clean(res))


# ══════════════════════════════════════════════════════════════════════════════
# Per-symbol cost, regime, flow, seasonality, Model Lab, Trade Desk
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/cost/symbols")
def cost_symbols():
    """Estimated Robinhood round-trip cost for every tradeable coin."""
    return ok(_clean(symbol_cost.estimate_universe()))


@router.get("/cost/symbols/{symbol}")
def cost_symbol(symbol: str):
    return ok(_clean(symbol_cost.estimate_symbol(symbol).to_dict()))


@router.get("/cost/coefficients")
def cost_coefficients():
    """Are the cost-model elasticities still priors, or have they been fitted?"""
    return ok(_clean(symbol_cost.fit_coefficients()))


@router.get("/regime")
def regime(refresh: bool = False):
    panel = engine.build_panel(refresh=refresh)
    return ok(_clean(regime_mod.regime_report(panel)))


@router.get("/flow")
def flow(refresh: bool = False):
    panel = engine.build_panel(refresh=refresh)
    if panel.T < 70:
        return ok({"available": False, "bars": panel.T,
                   "note": "needs at least ~70 bars of volume history"})
    return ok(_clean(regime_mod.flow_pressure(panel, panel.T - 1)))


@router.get("/strategies/days")
def strategies_days(start: str, end: str, mode: str = "paper"):
    """Each strategy day by day over [start, end]: trades, funnel, what changed,
    rank among peers, and the period league table. research/strategy_days.py."""
    from app.research import strategy_days as _sd
    return ok(_clean(_sd.report(start, end, mode)))


@router.get("/research/entry-lateness")
def entry_lateness_latest():
    """Latest lateness study per active strategy. research/entry_lateness.py."""
    from app.research import entry_lateness as _el
    from app.strategy.registry import studied
    return ok(_clean({"strategies": {s_: _el.latest(s_) for s_ in studied()}}))


@router.get("/research/entry-quality")
def entry_quality_latest():
    """Latest entry-quality fit per active strategy. research/entry_quality.py."""
    from app.research import entry_quality as _eq
    return ok(_clean(_eq.latest_all()))


@router.get("/research/regimes")
def regimes_latest():
    """The last daily regime study (day types, per-regime strategy scorecard),
    today's regime, and the router's multiplier per strategy. research/regime_days.py."""
    from app.research import regime_days as _rd
    from app.strategy.registry import studied
    res = _rd.latest() or {"available": False, "why": "the regime_days job has not run yet"}
    res = {k: v for k, v in res.items() if k != "model"}
    res["today"] = _rd.today() or res.get("today")
    res["router"] = {s_: _rd.multiplier(s_) for s_ in studied()}
    return ok(_clean(res))


@router.get("/strategies/versions")
def strategies_versions():
    """Every rule version each active strategy has traded under, head to head,
    with the stability readings that decide how older versions are weighted.
    research/versions.py."""
    from app.research import versions as _v
    return ok(_clean(_v.all_ledgers()))


@router.get("/research/rolling-entry")
def rolling_entry_latest():
    """The last daily answer to: does volume_build's trigger fire earlier on a
    rolling 60-minute window (pump_catch), and on more junk? See
    research/rolling_entry.py for the method and its stated assumptions."""
    from app.research import rolling_entry
    return ok(_clean(rolling_entry.latest() or {"available": False,
                                                 "why": "the rolling_entry job has not run yet"}))


@router.get("/research/day-shape")
def day_shape_latest():
    """The last daily bear-day study: rest-of-day outcomes when a coin's day has
    turned bear, across every coin-day in the history. research/day_shape.py."""
    from app.research import day_shape
    return ok(_clean(day_shape.latest() or {"available": False,
                                             "why": "the day_shape job has not run yet"}))


@router.get("/research/seasonality")
def seasonality(horizon_bars: int = 30, tz_offset_hours: float = -6.0):
    panel = engine.build_panel(refresh=False)
    return ok(_clean(calendar_effects(panel, horizon_bars=horizon_bars,
                                      tz_offset_hours=tz_offset_hours)))


@router.get("/research/model-lab/{strategy}")
def model_lab_report(strategy: str, walk_forward: bool = True,
                     cost_bps_per_side: float | None = None,
                     granularity: int = 3600):
    """The full lifecycle for one strategy: split, calibration, three passes,
    cost sensitivity, walk-forward, every acceptance gate, and the verdict."""
    if strategy not in STRATEGIES:
        raise HTTPException(404, f"unknown strategy {strategy!r}")
    # The heaviest request in the app: a 35,000-bar panel across every tracked
    # coin. Opening this page while the process was already large is one of the
    # ways macOS came to kill the backend mid-request -- which looked, from the
    # browser, like a tab that simply never loaded. Say no out loud instead.
    bars = 35000
    room = health_mod.headroom(health_mod.panel_cost_mb(bars, 80))
    if not room["ok"]:
        raise HTTPException(503, f"not enough memory headroom right now: {room['reason']}")
    panel = engine.build_panel(refresh=False, granularity=granularity, limit=bars)
    return ok(_clean(model_lab.run(strategy, panel,
                                   cost_bps_per_side=cost_bps_per_side,
                                   include_walk_forward=walk_forward)))


@router.get("/research/strategy-grid/{strategy}")
def strategy_grid(strategy: str):
    g = grid(strategy)
    n = 1
    for v in g.values():
        n *= len(v)
    return ok({"strategy": strategy, "grid": g, "n_configurations": n,
               "why_it_matters": ("Every configuration counted here is fed to the deflated "
                                  "Sharpe ratio. Searching more parameters raises the bar "
                                  "a result must clear.")})


@router.get("/desk")
def desk_state(mode: str = "advisory"):
    """Everything needed to act right now: open tickets and live positions."""
    return ok(_clean(desk.desk_state(mode)))


@router.post("/desk/{client_id}/skip")
def desk_skip(client_id: str, payload: dict = Body(default={})):
    return ok(_clean(desk.skip(client_id, payload.get("reason", "operator skipped"))))


@router.get("/strategies/operator")
def operator_strategies():
    """The four ideas the operator described, in his order, with their grids."""
    out = []
    for name in OPERATOR_STRATEGIES:
        s = build_strategy(name)
        g = grid(name)
        n = 1
        for v in g.values():
            n *= len(v)
        out.append({**s.describe(), "param_grid": g, "n_configurations": n})
    return ok(_clean(out))


# ══════════════════════════════════════════════════════════════════════════════
# Historical data, raw browser, attention, trade budget, per-strategy ledger
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/backfill/start")
def backfill_start(payload: dict = Body(default={})):
    """Download years of candles from Coinbase. Runs in the background."""
    return ok(_clean(backfill.start(
        granularity=int(payload.get("granularity", 3600)),
        days=float(payload.get("days", 1460)),
        symbols=payload.get("symbols"))))


@router.post("/backfill/cancel")
def backfill_cancel():
    return ok(_clean(backfill.cancel()))


@router.get("/backfill/status")
def backfill_status():
    return ok(_clean(backfill.status()))


@router.get("/backfill/coverage")
def backfill_coverage():
    """What history is actually stored — the source of truth for the date filters."""
    return ok(_clean(backfill.coverage()))


@router.get("/backfill/storage")
def backfill_storage(symbols: int = 80):
    return ok(_clean(backfill.storage_estimate(symbols)))


@router.get("/data/raw")
def raw_data(symbol: str, granularity: int = 3600,
             start_ts: float | None = None, end_ts: float | None = None,
             limit: int = 5000):
    return ok(_clean(backfill.raw_bars(symbol, granularity, start_ts, end_ts, limit)))


@router.get("/attention")
def attention(day_bars: int = 1440, refresh: bool = False):
    """Which coins are getting today's attention, and whose run is already late."""
    panel = engine.build_panel(refresh=refresh)
    return ok(_clean(attention_mod.attention(panel, day_bars=day_bars)))


@router.get("/attention/persistence")
def attention_persistence(day_bars: int = 1440, days: int = 20, top_k: int = 3):
    """Does yesterday's leader lead again today? Testable, and usually no."""
    panel = engine.build_panel(refresh=False)
    return ok(_clean(attention_mod.leadership_persistence(panel, day_bars, days, top_k)))


@router.post("/budget")
def create_budget(payload: dict = Body(...)):
    """e.g. {"slots": 2, "window_hours": 24} -- take at most 2 trades today,
    and hold out for the best ones rather than taking the first two."""
    return ok(_clean(budget.create(
        slots=int(payload["slots"]),
        window_hours=float(payload.get("window_hours", 24)),
        mode=payload.get("mode", "paper"),
        strategies=payload.get("strategies"),
        note=payload.get("note", ""))),
        "budget created")


@router.get("/budget")
def list_budgets(mode: str | None = None):
    return ok(_clean(budget.active(mode)))


@router.get("/budget/{budget_id}")
def get_budget(budget_id: int):
    b = budget.get(budget_id)
    if not b:
        raise HTTPException(404, "no such budget")
    return ok(_clean(b))


@router.post("/budget/{budget_id}/cancel")
def cancel_budget(budget_id: int):
    return ok(_clean(budget.cancel(budget_id)))


@router.get("/ledger/compare")
def ledger_compare(mode: str = "paper", start_ts: float | None = None,
                   end_ts: float | None = None):
    return ok(_clean(ledger.compare(mode, start_ts, end_ts)))


@router.get("/ledger/daily")
def ledger_daily(mode: str = "paper", days: int = 30):
    return ok(_clean(ledger.daily_pnl(mode, days)))


@router.get("/ledger/{strategy}")
def ledger_strategy(strategy: str, mode: str = "paper",
                    start_ts: float | None = None, end_ts: float | None = None):
    return ok(_clean(ledger.strategy_ledger(strategy, mode, start_ts, end_ts)))


# ══════════════════════════════════════════════════════════════════════════════
# Robinhood sync bridge
#
# Robinhood's agentic endpoint is an OAuth-gated MCP designed for an interactive
# LLM client, so this Python process cannot call it directly. The bridge instead
# runs the other way: a Claude Code session that HAS the MCP connected reads
# Robinhood and POSTs the results here. See docs/ROBINHOOD_SYNC.md for the exact
# prompt to paste. Nothing about this path can place an order.
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/universe/confirm")
def confirm_universe(payload: dict = Body(...)):
    """Mark symbols as genuinely tradeable on Robinhood.

    Only ever called with symbols the Robinhood MCP actually returned. Until a
    symbol is confirmed here it is shown as 'unverified' and live mode refuses it.
    """
    symbols = [str(s).upper().strip() for s in payload.get("symbols", []) if str(s).strip()]
    if not symbols:
        raise HTTPException(400, "symbols must be a non-empty list")
    n = universe.mark_rh_confirmed(symbols)

    unlisted = []
    if payload.get("deactivate_missing"):
        known = {r["symbol"] for r in universe.active_universe()}
        unlisted = sorted(known - set(symbols))
        for sym in unlisted:
            db.execute("UPDATE universe SET active=0, note=? WHERE symbol=?",
                       ("not returned by the Robinhood MCP", sym))

    db.log_event("INFO", "universe", f"Robinhood confirmed {n} symbols",
                 {"symbols": symbols, "deactivated": unlisted})
    return ok(_clean({
        "confirmed": n,
        "symbols": symbols,
        "deactivated": unlisted,
        "now_confirmed_total": db.query_one(
            "SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"],
        "note": ("These symbols are now eligible in live mode. Everything else stays "
                 "marked unverified and is refused."),
    }), "universe confirmed against Robinhood")


@router.post("/cost/observations/bulk")
def bulk_cost_observations(payload: dict = Body(...)):
    """Import several real fills at once (e.g. an order history pulled by an agent).

    Each item: symbol, side, notional_usd, fill_px, mid_at_submit [, mid_at_decision, ts].
    Real fills are the only thing that turns the cost model from belief into measurement.
    """
    items = payload.get("observations") or []
    if not items:
        raise HTTPException(400, "observations must be a non-empty list")
    written, errors = 0, []
    for i, o in enumerate(items):
        try:
            cost_model.record_observation(
                symbol=o["symbol"], side=o["side"],
                notional_usd=float(o["notional_usd"]), fill_px=float(o["fill_px"]),
                mid_at_submit=float(o["mid_at_submit"]),
                mid_at_decision=o.get("mid_at_decision"),
                quoted_spread_bps=o.get("quoted_spread_bps"),
                mode=o.get("mode", "manual"), source="measured")
            written += 1
        except Exception as exc:
            errors.append(f"item {i}: {type(exc).__name__}: {exc}")
    return ok(_clean({
        "written": written, "errors": errors,
        "global_estimate": cost_model.estimate().to_dict(),
        "coefficients": symbol_cost.fit_coefficients(),
    }), f"{written} real fills recorded")


# ══════════════════════════════════════════════════════════════════════════════
# False-breakout model
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/breakout/status")
def breakout_status():
    return ok(_clean(breakout_mod.status()))


@router.post("/breakout/train")
def breakout_train(payload: dict = Body(default={})):
    """Fit the false-breakout veto with purged walk-forward.

    Defaults to HOURLY bars — there are four years of those and only weeks of
    minute bars, and minute data cannot be backfilled from any public venue.
    """
    panel = engine.build_panel(refresh=False,
                               granularity=int(payload.get("granularity", 3600)),
                               limit=int(payload.get("limit", 35000)))
    rep = breakout_mod.load_or_train(panel, force=bool(payload.get("force", True)))
    return ok(_clean(rep))


@router.get("/breakout/report")
def breakout_report():
    rep = breakout_mod._MODEL.get("report")
    if not rep:
        return ok({"available": False,
                   "note": "not trained yet — POST /breakout/train"})
    return ok(_clean(rep))


@router.get("/breakout/veto/{symbol}")
def breakout_veto(symbol: str):
    """Is this coin breaking a level right now, and does the model expect it to fail?"""
    panel = engine.build_panel(refresh=False)
    return ok(_clean(breakout_mod.veto(panel, symbol.upper())))


@router.post("/breakout/threshold")
def breakout_threshold(payload: dict = Body(...)):
    return ok(_clean(breakout_mod.set_threshold(float(payload["threshold"]))))


@router.get("/breakout/events/{symbol}")
def breakout_events(symbol: str, limit: int = 100):
    """Recent labelled breakouts for one coin — the raw material of the model."""
    panel = engine.build_panel(refresh=False)
    evs = breakout_mod.find_breakouts(panel, symbol.upper())
    return ok(_clean({
        "symbol": symbol.upper(),
        "n": len(evs),
        "false_rate": (sum(1 for e in evs if e.label == 0) / len(evs)) if evs else None,
        "events": [e.to_dict() for e in evs[-limit:]],
    }))


# ══════════════════════════════════════════════════════════════════════════════
# News, events, and the research library
# ══════════════════════════════════════════════════════════════════════════════
@router.post("/news/fetch")
def news_fetch():
    return ok(_clean(news_mod.fetch()))


@router.get("/news")
def news(limit: int = 60, symbol: str | None = None,
         severity: str | None = None, hours: float | None = None):
    return ok(_clean(news_mod.recent(limit, symbol, severity, hours)))


@router.get("/news/alerts")
def news_alerts(hours: float = 24.0):
    """Coins in the middle of a hack, delisting or regulatory action — do not trade these."""
    return ok(_clean(news_mod.alerts(hours)))


@router.get("/library")
def research_library(status: str | None = None, tag: str | None = None):
    return ok(_clean(library.all_papers(status, tag)))


@router.post("/library")
def add_paper(payload: dict = Body(...)):
    try:
        return ok(_clean(library.add(payload)), "paper added")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# Execution mode
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/mode")
def get_mode():
    return ok(_clean(mode_mod.state()))


@router.post("/mode")
def set_mode(payload: dict = Body(...)):
    """Switch between paper and advisory freely. Live still requires the
    confirmation string in .env plus a restart — a toggle alone can never risk money."""
    try:
        res = mode_mod.set_mode(payload["mode"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return ok(_clean({**res, "state": mode_mod.state()}),
              res["reason"] if not res["changed"] else f"mode is now {res['mode']}")


@router.get("/setup")
def setup_state():
    """What is missing before this system can do anything useful."""
    # Both are full scans of a 3-million-row table, polled every few seconds
    # by every tab. Shared for two minutes (db.py note, 2026-09-21).
    bars = db.query_one_cached("SELECT COUNT(*) c FROM bars")["c"]
    span = db.query_one_cached("SELECT MIN(ts) a, MAX(ts) b FROM bars")
    days = ((span["b"] - span["a"]) / 86400) if span and span["a"] else 0
    from app.execution import rh_spread
    fills = db.query_one(
        "SELECT COUNT(*) c FROM cost_observations WHERE source='measured'")["c"]
    tradable_coins = db.query_one(
        "SELECT COUNT(*) c FROM universe WHERE active=1 AND rh_confirmed=1")["c"]
    spreads_measured = db.query_one(
        """SELECT COUNT(*) c FROM rh_spreads s JOIN universe u ON u.symbol = s.symbol
           WHERE u.active=1 AND u.rh_confirmed=1""")["c"]
    confirmed = db.query_one("SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"]
    eng = engine.status()

    steps = [
        {"key": "universe", "label": "Coin list loaded",
         "done": db.query_one("SELECT COUNT(*) c FROM universe")["c"] > 0,
         "detail": f"{db.query_one('SELECT COUNT(*) c FROM universe')['c']} coins",
         "action": None},
        {"key": "history", "label": "Price history downloaded",
         "done": bars > 50000,
         "detail": (f"{bars:,} bars, {days:.1f} days" if bars else "nothing yet"),
         "action": "Data → start backfill (1 hour / 4 years)",
         "why": "Every strategy is data-starved until this runs. It is the one step that unblocks everything else."},
        {"key": "engine", "label": "Engine running",
         "done": bool(eng.get("running")),
         "detail": (f"{eng.get('ticks', 0)} ticks" if eng.get("running") else "stopped — collecting nothing"),
         "action": "Press Start on this page",
         "why": "The engine is what records live quotes, generates signals and books paper trades. Nothing accumulates while it is stopped."},
        # This used to read "Real fills logged: 0 of 20" and count rows in
        # cost_observations with source='measured'. Paper fills are deliberately
        # recorded as source='prior' -- they are simulated and must never be
        # mistaken for measurement -- so the step could NEVER complete, and it sat
        # on the front page as a permanent instruction that does nothing.
        #
        # What is actually worth doing is the thing the button beside it does:
        # read Robinhood's own bid/ask per coin, which replaces the 0.95% default
        # with a measured number. That is a real gap and it is completable.
        {"key": "spreads", "label": "Spread measured per coin",
         "done": spreads_measured >= tradable_coins and tradable_coins > 0,
         "detail": (f"{spreads_measured} of {tradable_coins} tradable coins measured"
                    + (f"; {tradable_coins - spreads_measured} still on the "
                       f"{rh_spread.DEFAULT_SPREAD_PCT}% default"
                       if tradable_coins > spreads_measured else "")),
         "action": "",
         "why": ("Runs by itself once a day — nothing for you to do. Cost is the largest "
                 "term in every result, so each coin is priced at its own measured spread "
                 "once Robinhood has been asked; until then it uses the published "
                 "default, which is close but not that coin's own number.")},
        {"key": "confirmed", "label": "Coins confirmed on Robinhood",
         "done": confirmed > 0,
         "detail": f"{confirmed} confirmed",
         "action": "docs/ROBINHOOD_SYNC.md",
         "why": "Live mode refuses any coin the Robinhood MCP has not confirmed."},
    ]
    blocking = [s for s in steps if not s["done"]]
    return ok(_clean({
        "steps": steps,
        "ready": not blocking,
        "next": blocking[0] if blocking else None,
        "counts": {"bars": bars, "history_days": days, "measured_fills": fills,
                   "confirmed_symbols": confirmed},
    }))


# ══════════════════════════════════════════════════════════════════════════════
# Account, universe selection, macro, scheduler, paper feed
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/account")
def account():
    return ok(_clean(mode_mod.equity_state()))


@router.post("/account")
def set_account(payload: dict = Body(...)):
    """Record what you actually funded. Nothing here reads your Robinhood balance."""
    try:
        return ok(_clean(mode_mod.set_equity(float(payload["equity_usd"]),
                                             payload.get("note", ""))),
                  "account equity updated — sizing and risk limits now use this number")
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@router.get("/universe/selection")
def universe_selection(dry_run: bool = True):
    """Why each coin is tracked, traded, or dropped."""
    return ok(_clean(selection.review(dry_run=dry_run)))


@router.post("/universe/review")
def universe_review(payload: dict = Body(default={})):
    return ok(_clean(selection.review(payload.get("config"), dry_run=False)),
              "universe roles updated")


@router.get("/universe/history")
def universe_history(limit: int = 100):
    return ok(_clean(selection.history(limit)))


@router.get("/macro")
def macro_latest():
    return ok(_clean(macro_mod.latest()))


@router.post("/macro/fetch")
def macro_fetch():
    return ok(_clean(macro_mod.fetch()))


@router.get("/macro/correlations")
def macro_correlations(days: int = 365):
    return ok(_clean(macro_mod.correlations(days)))


@router.get("/prediction-markets")
def prediction_markets_latest():
    """Polymarket's crypto odds, read as today's implied BTC/ETH distribution
    plus the busiest month/year markets. Context, not a signal."""
    from app.data import prediction_markets as _pm
    return ok(_clean(_pm.latest()))


@router.post("/prediction-markets/fetch")
def prediction_markets_fetch():
    from app.data import prediction_markets as _pm
    return ok(_clean(_pm.fetch()))


@router.get("/scheduler")
def scheduler_status():
    return ok(_clean(scheduler.status()))


@router.post("/scheduler/start")
def scheduler_start():
    return ok(_clean(scheduler.start()), "automatic research started")


@router.post("/scheduler/stop")
def scheduler_stop():
    return ok(_clean(scheduler.stop()), "automatic research stopped")


@router.post("/scheduler/pause")
def scheduler_pause(payload: dict = Body(default={})):
    return ok(_clean(scheduler.pause(bool(payload.get("paused", True)))))


@router.post("/scheduler/job/{name}/run")
def scheduler_run(name: str):
    try:
        return ok(_clean(scheduler.run_job(name)))
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@router.post("/scheduler/job/{name}/enabled")
def scheduler_enable(name: str, payload: dict = Body(...)):
    return ok(_clean(scheduler.set_enabled(name, bool(payload["enabled"]))))


@router.post("/scheduler/job/{name}/interval")
def scheduler_interval(name: str, payload: dict = Body(...)):
    return ok(_clean(scheduler.set_interval(name, float(payload["seconds"]))))


@router.post("/library/fetch")
def library_fetch():
    return ok(_clean(papers_feed.fetch()), "paper feed refreshed")


# ══════════════════════════════════════════════════════════════════════════════
# Process health and disk housekeeping
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/health/report")
def health_report():
    """Memory, disk and uptime. The overnight crash was almost certainly memory."""
    return ok(_clean(health_mod.report()))


@router.get("/system/threads")
def system_threads():
    """Every thread's stack right now, who holds the database lock, and how
    long callers have been waiting for it. Added 2026-09-21 when /mode took
    27 s and /health/report (no database) took 24 s: the process was starved
    and nothing said by what. Cheap; no database access of its own."""
    import sys
    import threading
    import traceback
    from pathlib import Path
    from app.core import db as _db
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    stacks = {}
    for ident, frame in frames.items():
        name = names.get(ident, str(ident))
        if name == threading.current_thread().name:
            continue
        tb = traceback.extract_stack(frame)[-10:]
        stacks[name] = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in tb]
    holder = dict(_db.LOCK_HOLDER)
    if holder.get("since"):
        holder["held_for_s"] = round(time.time() - holder["since"], 3)
    return ok(_clean({
        "lock_holder": holder,
        "lock_waits": dict(_db.LOCK_WAITS),
        "threads": len(names),
        "tick_profile_last": engine.tick_profile(),
        "stacks": stacks,
    }))


@router.get("/housekeeping/preview")
def housekeeping_preview():
    """What retention would delete, without deleting it."""
    return ok(_clean(housekeeping.run(dry_run=True)))


@router.post("/housekeeping/run")
def housekeeping_run():
    return ok(_clean(housekeeping.run(dry_run=False)), "housekeeping complete")


# ---------------------------------------------------------------- strategy control
# The operator's own switch. Built 2026-09-21 after burst_catch lost $14.39 in
# three hours with no way to stop it from the app. Read the module docstring in
# app/risk/control.py for why entries are refused but exits never are.

@router.get("/strategies/control")
def strategies_control():
    from app.risk import control
    from app.core import mode as _mode
    m = _mode.get_mode()
    rows = control.roster(m)
    # The record each switch should be judged against, so the decision to turn
    # something off is made next to the reason to, not from memory.
    for r in rows:
        rec = db.query_one(
            """SELECT COUNT(*) n, SUM(CASE WHEN net_pnl_usd>0 THEN 1 ELSE 0 END) w,
                      COALESCE(SUM(net_pnl_usd),0) net, COALESCE(SUM(cost_usd),0) cost,
                      COALESCE(SUM(gross_pnl_usd),0) gross
               FROM trades WHERE strategy=? AND mode=? AND ts_close IS NOT NULL""",
            (r["strategy"], m))
        r["closed"] = int(rec["n"] or 0) if rec else 0
        r["wins"] = int(rec["w"] or 0) if rec else 0
        r["net_usd"] = float(rec["net"] or 0.0) if rec else 0.0
        r["gross_usd"] = float(rec["gross"] or 0.0) if rec else 0.0
        r["cost_usd"] = float(rec["cost"] or 0.0) if rec else 0.0
        op = db.query_one(
            "SELECT COUNT(*) n FROM positions WHERE strategy=? AND qty>0", (r["strategy"],))
        r["open_positions"] = int(op["n"] or 0) if op else 0
    return ok(_clean({"mode": m, "strategies": rows}))


@router.post("/strategies/control/{name}")
def strategies_control_set(name: str, payload: dict = Body(...)):
    from app.risk import control
    from app.strategy.registry import STRATEGIES
    if name not in STRATEGIES:
        raise HTTPException(status_code=404, detail=f"no strategy named {name}")
    st = control.set_state(
        name,
        enabled=None if payload.get("enabled") is None else bool(payload["enabled"]),
        daily_budget_usd=None if payload.get("daily_budget_usd") is None
                         else float(payload["daily_budget_usd"]),
        max_position_usd=None if payload.get("max_position_usd") is None
                         else float(payload["max_position_usd"]),
        note=payload.get("note"),
        source=control.SOURCE_OPERATOR)
    what = []
    if payload.get("enabled") is not None:
        what.append("switched " + ("on" if payload["enabled"] else "off"))
    if payload.get("daily_budget_usd") is not None:
        b = float(payload["daily_budget_usd"])
        what.append(f"daily budget ${b:.2f}" if b > 0 else "daily budget removed")
    if payload.get("max_position_usd") is not None:
        c = float(payload["max_position_usd"])
        what.append(f"per-trade cap ${c:.2f}" if c > 0 else "per-trade cap removed")
    return ok(_clean(st), f"{name}: " + (", ".join(what) if what else "unchanged"))


# ---------------------------------------------------------------- retire to the lab
# "Remove its trades from paper but keep them to train on." Nothing is deleted:
# the rows move to mode='lab', which the book ignores and exit_lab still reads.
# See app/research/retire.py for why that one change does both halves.

@router.get("/strategies/{name}/retire-preview")
def strategy_retire_preview(name: str):
    from app.research import retire
    from app.core import mode as _mode
    return ok(_clean(retire.preview(name, _mode.get_mode())))


@router.post("/strategies/{name}/retire")
def strategy_retire(name: str, payload: dict = Body(default={})):
    from app.research import retire
    from app.strategy.registry import STRATEGIES
    from app.core import mode as _mode
    if name not in STRATEGIES:
        raise HTTPException(status_code=404, detail=f"no strategy named {name}")
    res = retire.retire(name, str(payload.get("reason") or "retired by the operator"),
                        _mode.get_mode())
    if not res.get("ok"):
        # A refusal is not a server error -- it is the answer. 409 so the UI can
        # show which positions would not close rather than a generic failure.
        raise HTTPException(status_code=409, detail=res)
    moved = res.get("rows_moved", {})
    return ok(_clean(res),
              f"{name} retired to the lab: {moved.get('trades', 0)} trade(s) moved, "
              f"${res.get('net_removed_from_book_usd', 0.0):+.2f} removed from the book")


@router.post("/strategies/{name}/restore")
def strategy_restore(name: str):
    from app.research import retire
    from app.core import mode as _mode
    res = retire.restore(name, _mode.get_mode())
    return ok(_clean(res), f"{name} restored to {_mode.get_mode()}")


@router.get("/strategies/retired")
def strategies_retired():
    from app.research import retire
    from app.core import mode as _mode
    return ok(_clean({"retired": retire.retired(_mode.get_mode())}))


@router.get("/lab/daily")
def lab_daily(mode: str | None = None, days: int = 30):
    """The lab's daily scorecard and what it switched off."""
    from app.research import daily_lab
    from app.core import mode as _m
    return ok(_clean(daily_lab.history(mode or _m.get_mode(), int(days))))


@router.get("/signal-races")
def signal_races(mode: str | None = None, limit: int = 40):
    """Every signal that competed in a bar where one was taken, plus the recipe
    the strategy ranked them by."""
    from app.research import signal_race
    from app.core import mode as _m
    return ok(_clean({"races": signal_race.races(mode or _m.get_mode(), int(limit))}))


@router.post("/trade-plans/backfill")
def trade_plans_backfill(payload: dict = Body(default={})):
    """Rebuild a plan for every past trade that does not have one."""
    from app.research import trade_plan
    from app.core import mode as _m
    return ok(_clean(trade_plan.backfill(payload.get("mode") or _m.get_mode(),
                                         int(payload.get("limit") or 500))))


@router.get("/trade-plans/accuracy")
def trade_plans_accuracy(mode: str | None = None):
    """Per strategy: how often the prediction was right, against how often it
    said it would be."""
    from app.research import trade_plan
    from app.core import mode as _m
    return ok(_clean({"rows": trade_plan.accuracy(mode or _m.get_mode())}))


@router.get("/trade-plans")
def trade_plans(mode: str | None = None, limit: int = 200):
    """The plan each trade was taken on, and how it has been graded since."""
    from app.research import trade_plan
    from app.core import mode as _m
    return ok(_clean({"plans": trade_plan.live(mode or _m.get_mode(), int(limit))}))


@router.get("/positions/path")
def positions_path():
    """How far up and how far down each open position has been, net of both sides.

    The Risk tab answers "what is it worth now". This answers "what was the best
    and the worst it has been, and when" -- which is the number the exit argument
    actually turns on. See app/research/position_path.py.
    """
    from app.research import position_path
    from app.core import mode as _mode
    return ok(_clean(position_path.open_paths(_mode.get_mode())))


@router.get("/research/coin-stacking")
def research_coin_stacking(days: float = 30.0):
    """What the entries blocked by the coin_stacking pause would have returned.

    Paused in the book 2026-09-22, kept in the lab so the question is settled
    with numbers. See app/research/coin_stacking.py.
    """
    from app.research import coin_stacking
    return ok(_clean(coin_stacking.study(days)))
