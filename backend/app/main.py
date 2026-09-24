from __future__ import annotations

import logging
import os
import time

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.routes import router
from app.config import get_settings
from app.core import access, db, dbguard, health, registry, scheduler

s = get_settings()
logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tradecrypto")

@asynccontextmanager
async def lifespan(_: FastAPI):
    path = db.init_db()
    # Hold the claim for as long as we run, so nothing on another machine can
    # open this file read-write behind us.
    dbguard.start_heartbeat(path, role="tradecrypto-app")
    registry.bootstrap_cards()
    rec = db.LAST_RECOVERY
    if rec is not None and rec.restored:
        log.warning("%s", rec.headline())
        db.log_event("ERROR", "system", rec.headline(),
                     {"restored_from": str(rec.restored_from),
                      "quarantined": str(rec.quarantined),
                      "lost_since": rec.lost_since})
    # ANYTHING THAT CHANGES WHAT THE DESK MAY SPEND RUNS HERE, SYNCHRONOUSLY,
    # before the engine ticks once.
    #
    # These two used to sit at the end of the background catch-up thread, behind
    # up to twelve history-backfill passes. On 2026-09-17 that meant the operator
    # restarted, opened Overview, and saw equity of $488 against a declared stake
    # of $2,000 -- because the reconcile was queued behind six minutes of candle
    # fetching and had not run yet. Configuration that decides position sizes
    # cannot be somewhere in a queue; it is either applied before the first trade
    # or it is not applied.
    try:
        from app.core import setup_ops as _ops
        _st = _ops.reconcile_declared_stake()
        if _st.get("deposited"):
            log.info("paper book topped up by $%.2f to the declared stake",
                     _st["deposited"])
        elif _st.get("error"):
            log.warning("could not reconcile the declared stake: %s", _st["error"])
        # Ids first: the recovery below inserts trades, and it must never be
        # handed an id that a destroyed trade already used.
        _hw = _ops.enforce_trade_id_high_water_mark()
        if _hw.get("bumped"):
            log.warning("trade id counter was behind by %d; pushed forward so no id repeats",
                        _hw["high_water"] - _hw["max_now"])
        _rc = _ops.restore_lost_trades_once()
        if _rc.get("restored") or _rc.get("corrected"):
            log.warning("recovered %d lost trade(s), corrected %d fabricated close(s)",
                        _rc.get("restored", 0), _rc.get("corrected", 0))
        # Again, because the recovery above inserts trades: the mark is a record
        # of the highest id ever issued, so it has to be taken after the last
        # write, not only before the first.
        _ops.enforce_trade_id_high_water_mark()
        try:
            from app.research import versions as _versions
            _nv = _versions.backfill_untagged()
            if _nv:
                log.info("tagged %d earlier trade(s) with the rule version that made them (backfilled, marked *)", _nv)
        except Exception as exc:
            log.warning("version tagging skipped: %s", exc)
        _dg = _ops.declare_data_gaps_once()
        if _dg.get("error"):
            log.warning("could not declare data gaps: %s", _dg["error"])
        _bl = _ops.backfill_trade_links_once()
        _ct = _ops.clear_sentinel_targets_once()
        if _ct.get("cleared"):
            log.warning("cleared %d impossible exit target(s): %s",
                        _ct["cleared"], ", ".join(_ct.get("which") or []))
        if _bl.get("linked"):
            log.info("recovered order linkage on %d trade(s)", _bl["linked"])

        # Any closed trade without a verdict gets one. Cheap, idempotent, and
        # deliberately NOT a run-once flag: reattribute_trades_once() was one,
        # and it had already marked itself done before the trades salvaged out
        # of the corrupt database were inserted -- so four rows sat in the
        # Journal showing "—" where the verdict goes, with nothing left in the
        # system that would ever fix them (blueprint 5.3).
        from app.feedback import loop as _fb
        _lab = _fb.label_unlabelled_trades()
        if _lab.get("labelled"):
            log.info("labelled %d trade(s) that had no verdict", _lab["labelled"])

        # The vault catches up on anything its synchronous write missed. This is
        # what makes "every order is in the vault" a checked fact rather than a
        # promise -- see core/vault.py.
        # Give the mechanism counters the history that already exists, so the
        # one mechanism that has genuinely never run stands out instead of
        # being buried in four that were only just instrumented.
        from app.core import liveness as _live
        _seed = _live.seed_from_history()
        if _seed:
            log.info("liveness: seeded %s from the ledger",
                     ", ".join(f"{k}={v}" for k, v in _seed.items()))
        _never = [m["name"] for m in _live.report()["mechanisms"] if m["alarming"]]
        if _never:
            log.warning("MECHANISMS THAT HAVE NEVER RUN: %s", ", ".join(_never))

        from app.core import vault as _vault
        _v = _vault.reconcile()
        if _v.get("added"):
            log.info("vault: appended %d record(s) it did not have (%s)",
                     _v["added"], _v["dir"])
        if _v.get("failed") or _v.get("error"):
            log.warning("vault: %s failed, %s", _v.get("failed"), _v.get("error"))
    except Exception as exc:
        log.warning("boot repairs failed: %s", exc)

    db.log_event("INFO", "system", f"backend started, mode={s.execution_mode}")
    log.info("database at %s", path)
    log.info("execution mode: %s (live_enabled=%s)", s.execution_mode, s.live_enabled)
    if access.lan_enabled():
        import socket
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            probe.close()
        except Exception:
            ip = "your-mac-ip"
        log.info("LAN access is ON. On your phone, open:")
        log.info("    http://%s:%s/?token=%s", ip, os.environ.get("FRONTEND_PORT", "5180"),
                 access.token())
        log.info("The page stores the token, so you only type that link once.")
        db.log_event("INFO", "system",
                     f"LAN access enabled; phone URL http://{ip}:"
                     f"{os.environ.get('FRONTEND_PORT', '5180')}/?token=…")
    if not s.live_enabled:
        log.info("real orders are DISABLED -- this is the safe default")
    # The memory watchdog is the guard that turns an out-of-memory kill into a
    # clean, restartable exit. It once failed silently for days because the
    # route function below was named `health` and shadowed this module, so
    # `health.start_watch()` raised AttributeError straight into a `warning`
    # nobody read. A guard that can fail quietly is not a guard: if this does
    # not come up, it is recorded in the database as an ERROR event and shows
    # on the dashboard.
    try:
        health.start_watch()
        if not health.watch_alive():
            raise RuntimeError("start_watch() returned but no watcher thread is alive")
        d = health.disk()
        log.info("disk: %.0f MB used by this project, %.1f GB free",
                 d.get("project_mb") or 0, d.get("free_gb") or 0)
        if d.get("low_space"):
            log.warning("LOW DISK: %s", d["warning"])
            db.log_event("WARN", "health", d["warning"])
    except Exception as exc:
        log.error("MEMORY WATCHDOG DID NOT START: %s: %s", type(exc).__name__, exc)
        try:
            db.log_event("ERROR", "health",
                         f"memory watchdog did not start ({type(exc).__name__}: {exc}) — "
                         f"the process is running WITHOUT its out-of-memory guard")
        except Exception:
            pass
    try:
        scheduler.start()
        log.info("automatic research scheduler started")
    except Exception as exc:
        log.warning("scheduler did not start: %s", exc)

    # Start the engine. It had to be started by hand after every restart, so a
    # crash at 2am meant no data and no paper trades until somebody noticed and
    # pressed a button. Never in live mode: real money is started deliberately
    # or not at all.
    try:
        # Adopt every coin Robinhood will actually trade. RAY, XTZ, AERO and SKL
        # all moved on days he pointed at and none of them were in the universe,
        # because the seed list was a hand-typed guess. This has cost four
        # separate opportunities now; it should not need a button.
        try:
            from app.data import universe as _u
            _a = _u.adopt_from_robinhood()
            if _a.get("added_count"):
                log.info("universe: adopted %s new tradable coins: %s",
                         _a["added_count"], ", ".join(_a["added"][:20]))
                db.log_event("INFO", "universe",
                             f"adopted {_a['added_count']} coins Robinhood trades that we "
                             f"were not watching: {', '.join(_a['added'][:25])}")
        except Exception as exc:
            log.warning("universe adoption skipped: %s: %s", type(exc).__name__, exc)

        # ── catch up, by itself ──────────────────────────────────────────────
        #
        # Every one of these was a manual click the operator was told to make:
        # deepen the thin coins, re-run the universe review so the newly-deep
        # coins get promoted, and re-run the jobs whose last result is a stale
        # error. A desk that needs a five-step checklist after every restart is
        # not automated, it is a chore with a dashboard.
        #
        # Runs in the background so boot is not blocked, one job at a time so a
        # heavy one never competes with live data collection.
        def _catch_up() -> None:
            import time as _t
            from app.core import scheduler as _sched
            _t.sleep(20)                     # let the engine and feed settle first

            # FIRST, before the history top-up. The backup check was originally
            # written after it, which queued the one step you would actually miss
            # behind twelve passes of the one you would not: history deepening
            # runs ~80s per pass, so an overdue backup sat unmade for a quarter
            # of an hour after every restart. Catch-up runs in the order of what
            # is worst to be without.
            try:
                # A daily backup that has not landed in over a day is the one
                # job you cannot afford to wait a full interval to retry. On
                # 2026-09-18 the backup had been failing for a day with
                # "cannot VACUUM from within a transaction" while the job
                # recorded status "ok", so nothing re-ran it and nothing said a
                # word. Age of the FILE is the honest test: it does not depend
                # on the job having told the truth about itself.
                try:
                    from app.core import housekeeping as _hk
                    _backups = _hk.list_backups()
                    _age_h = ((time.time() - _backups[0]["mtime"]) / 3600.0
                              if _backups else 1e9)
                    if _age_h > 26:
                        db.log_event("WARNING", "startup",
                                     f"newest full backup is {_age_h:.0f}h old — "
                                     f"taking one now")
                        _b = _hk.backup("boot — the daily backup was overdue")
                        db.log_event("INFO" if _b.get("ok") else "ERROR", "startup",
                                     (f"catch-up backup {_b.get('megabytes', 0):.0f} MB"
                                      if _b.get("ok")
                                      else f"catch-up backup FAILED: {_b.get('error')}"))
                except Exception as exc:
                    db.log_event("WARNING", "startup",
                                 f"backup-age check failed: {type(exc).__name__}: {exc}")

            except Exception:
                pass

            try:
                # Deepen until nothing thin is left, not five per run.
                for _pass in range(12):
                    thin = db.query(
                        """SELECT COUNT(*) n FROM (
                             SELECT u.symbol FROM universe u
                             LEFT JOIN bars b ON b.symbol=u.symbol AND b.granularity=3600
                             WHERE u.active=1 GROUP BY u.symbol HAVING COUNT(b.ts) < 4000)""")
                    left = (thin[0]["n"] if thin else 0)
                    if not left:
                        break
                    db.log_event("INFO", "startup",
                                 f"catch-up pass {_pass + 1}: {left} coin(s) still short on history")
                    _sched.run_job("history_topup")
                # Only what is actually overdue. These three are scheduled jobs
                # with their own cadences; running them on every boot turned a
                # ten-second restart into minutes of redundant work.
                def _if_due(job: str) -> None:
                    if _sched.due(job):
                        _sched.run_job(job)
                    else:
                        log.info("catch-up: %s is not due — skipping", job)

                # Now that history is in, decide roles on it.
                _if_due("universe_review")
                # Learn the clock from data before the first tick, so hour
                # weights are never a stale constant left over from a fit.
                _if_due("hour_profile")
                # Fill in every day we have bars for but no report yet.
                _if_due("daily_report")
                _sched.run_job("relearn")
                try:
                    _ops2 = __import__("app.core.setup_ops", fromlist=["x"])
                    _rc = _ops2.reclassify_reports_once()
                    if _rc.get("days"):
                        db.log_event("INFO", "startup",
                                     f"reclassified {_rc['days']} report(s) by shape")
                except Exception:
                    pass
                _sched.run_job("evolve")
                # Any recent day whose report was built before its last trade
                # landed. Cheap (one indexed MAX per day) and it means a restart
                # is enough to make the Daily tab agree with the Journal, rather
                # than waiting for someone to open that exact day or for the
                # six-hourly job to come round.
                try:
                    from app.research import daily_report as _dr
                    _fixed = []
                    for _d in (_dr.available_days(14) or []):
                        _row = db.query_one(
                            "SELECT built_at FROM daily_reports WHERE day=?", (_d,))
                        if _row and _dr._last_activity(_d) > float(_row["built_at"] or 0):
                            _dr.get(_d)
                            _fixed.append(_d)
                    if _fixed:
                        db.log_event("INFO", "report",
                                     f"rebuilt {len(_fixed)} stale day(s): {', '.join(_fixed)}")
                except Exception as exc:
                    db.log_event("WARNING", "report",
                                 f"stale-day sweep failed: {type(exc).__name__}: {exc}")

                # Clear any job sitting on a stale failure.
                for row in db.query(
                        "SELECT name FROM scheduler_jobs WHERE last_status='error'"):
                    try:
                        _sched.run_job(row["name"])
                    except Exception:
                        pass            # a job that fails again is reported by the job itself
                db.log_event("INFO", "startup", "catch-up finished; no manual steps needed")
            except Exception as exc:
                db.log_event("WARNING", "startup",
                             f"catch-up stopped early: {type(exc).__name__}: {exc}")

        try:
            import threading as _threading
            _threading.Thread(target=_catch_up, name="tc-catchup", daemon=True).start()
            log.info("catch-up running in the background: history, universe review, failed jobs")
        except Exception as exc:
            log.warning("catch-up did not start: %s: %s", type(exc).__name__, exc)

        # How long was the desk actually down? A planned restart should cost
        # seconds; if it starts costing minutes, that is a regression worth
        # seeing rather than guessing at.
        try:
            _ps = db.query_one("SELECT value FROM app_state WHERE key='planned_shutdown_ts'")
            if _ps and _ps["value"]:
                _gap = time.time() - float(_ps["value"])
                if 0 < _gap < 3600:
                    log.info("back up after a planned restart — %.0fs of downtime", _gap)
                    from app.core import journal as _j
                    _j.append("restart_completed", {"downtime_s": round(_gap, 1)})
                db.execute("DELETE FROM app_state WHERE key='planned_shutdown_ts'")
        except Exception:
            pass

        from app.core import code_version as _cv
        try:
            _cv.snapshot_boot()
        except Exception as exc:
            log.warning("code-version snapshot failed: %s", exc)

        from app.core import setup_ops as _ops
        try:
            _fix = _ops.repair_double_counted_pnl_once()
            if _fix.get("trades_fixed"):
                log.info("re-booked %s trade(s) charged the spread twice; realised P&L %+.2f",
                         _fix["trades_fixed"], _fix.get("pnl_delta_usd", 0.0))
        except Exception as exc:
            log.error("P&L repair failed: %s", exc)
        try:
            from app.feedback import defects as _defs
            _defs.close_window_once()
        except Exception as exc:
            log.warning("defect window stamp failed: %s", exc)
        try:
            _v = _ops.reattribute_trades_once()
            if _v.get("trades"):
                log.info("re-labelled %s trade verdict(s) after the attribution fix", _v["trades"])
        except Exception as exc:
            log.error("verdict re-label failed: %s", exc)
        try:
            _r = _ops.reset_paper_book_once()
            if _r.get("reset"):
                log.info("paper book cleared once on boot: %s", _r.get("cleared"))
        except Exception as exc:
            log.error("one-time paper book reset failed: %s", exc)

        from app.execution import engine
        if not engine.wanted():
            log.info("engine left stopped by the operator; not autostarting")
            db.log_event("INFO", "engine",
                         "not autostarted — it was stopped deliberately and that is "
                         "remembered. Press start on the Overview page to resume.")
        elif getattr(s, "autostart_engine", True) and not s.live_enabled:
            st = engine.start()
            log.info("engine autostarted in %s mode, strategies=%s",
                     s.execution_mode, st.get("strategies"))
            db.log_event("INFO", "engine",
                         f"autostarted on boot in {s.execution_mode} mode")
        elif s.live_enabled:
            log.info("live mode: the engine is NOT autostarted, start it deliberately")
    except Exception as exc:
        log.error("engine failed to autostart: %s: %s", type(exc).__name__, exc)
        try:
            db.log_event("ERROR", "engine",
                         f"autostart failed ({type(exc).__name__}: {exc}) — "
                         f"nothing is being collected or traded")
        except Exception:
            pass
    yield
    try:
        scheduler.stop()
    except Exception:
        pass


app = FastAPI(
    lifespan=lifespan,
    title="TradeCrypto",
    version="0.1.0",
    description=(
        "A Robinhood crypto trading system built so that every number on screen "
        "can be traced to the model, the data and the assumptions that produced it."
    ),
)
# Access from anything that is not this machine is gated. See core/access.py:
# binding to the LAN so the phone can reach it also makes this process reachable
# by everything else on the network, and it holds broker credentials.
app.middleware("http")(access.guard)
app.add_middleware(
    CORSMiddleware,
    # With LAN access on, the phone's origin is not in the configured list and a
    # browser would block every call before the token was ever checked.
    allow_origins=(["*"] if access.exposed() else s.origins),
    allow_credentials=not access.exposed(),
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(router)


# NOTE: do not name this `health`. That rebinds the `health` module imported at
# the top of this file, and every later `health.<anything>` fails at runtime.
@app.get("/health")
def health_check() -> dict:
    return {
        "status": "ok",
        "mode": s.execution_mode,
        "live_enabled": s.live_enabled,
        "pid": os.getpid(),
        "memory_watchdog": health.watch_alive(),
    }
