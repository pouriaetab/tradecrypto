"""Everything setup needs to do, exposed so the web app can do it.

Why this exists
---------------
The operator asked, correctly and more than once, for this to be a web app. Yet
every new capability arrived with a list of terminal commands attached. That was
not a technical necessity — the backend runs on his machine, as him. It can
install its own dependencies, generate its own keys and restart itself. It simply
had never been asked to.

So: buttons, not commands. The only thing left for a person to do is the one thing
a person must do — sign in to Robinhood and paste a public key, because that is
their account and their credentials.

Safety
------
The package installer takes a name from a fixed allowlist, never from the caller.
The API binds to 127.0.0.1 only. Private keys are generated here, written with
0600 permissions, and never returned, logged or echoed back.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
import subprocess
import sys
from pathlib import Path

from app.config import get_settings
from app.core import db

BACKEND = Path(__file__).resolve().parents[2]
PROJECT = BACKEND.parent

# A caller may ask for these and nothing else. An installer that takes an
# arbitrary package name from an HTTP request is a remote code execution hole,
# localhost or not.
INSTALLABLE = {
    "pynacl": {
        "spec": "pynacl==1.5.0",
        "why": "Ed25519 signing for the Robinhood API. Nothing else needs it.",
        "import_name": "nacl",
    },
}


def _venv_python() -> str:
    p = BACKEND / ".venv" / "bin" / "python"
    return str(p) if p.exists() else sys.executable


def _installed(import_name: str) -> bool:
    try:
        __import__(import_name)
        return True
    except ImportError:
        return False


def credentials_path() -> Path:
    """The one place this path is decided. Both the setup page and the API
    client read it from here, because when they each resolved it themselves
    they disagreed and the credentials became invisible."""
    return get_settings().rh_credentials_file


def status() -> dict:
    """What is missing, in the order it has to be fixed."""

    have_nacl = _installed("nacl")
    cred = credentials_path()
    has_creds = False
    api_key_hint = None
    if cred.exists():
        try:
            d = json.loads(cred.read_text())
            has_creds = bool(d.get("api_key") and d.get("private_key"))
            k = d.get("api_key") or ""
            api_key_hint = (k[:11] + "…" + k[-4:]) if len(k) > 18 else ("set" if k else None)
        except Exception:
            has_creds = False

    reachable, err, account = None, None, None
    if have_nacl and has_creds:
        # no broker integration in this build — the order-placing and account code was removed when this repository was published. Credentials on disk are never used and never sent anywhere.
        reachable, err = None, "no broker integration in this build — the order-placing and account code was removed when this repository was published"

    confirmed = db.query_one("SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"]
    measured = db.query_one(
        "SELECT COUNT(*) c FROM rh_spreads WHERE source='observed'")["c"] \
        if db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='rh_spreads'") else 0

    steps = [
        {"key": "pynacl", "n": 1, "label": "Install the signing library",
         "done": have_nacl,
         "detail": "PyNaCl is present" if have_nacl else "PyNaCl is not installed",
         "button": None if have_nacl else "install",
         "why": "Robinhood signs every request with an Ed25519 key. This is the "
                "only thing that can do that maths."},
        {"key": "keypair", "n": 2, "label": "Create a key pair",
         "done": has_creds, "blocked": not have_nacl,
         "detail": "credentials are saved" if has_creds else "no key pair yet",
         "button": None if has_creds else "generate",
         "why": "The private half stays on this machine and is never sent anywhere. "
                "You give Robinhood only the public half."},
        {"key": "robinhood", "n": 3, "label": "Paste the public key into Robinhood",
         "done": has_creds and reachable is True,
         "blocked": not has_creds,
         "detail": ("connected"
                    if reachable else (err or "waiting for your API key")),
         "button": None,
         "manual": True,
         "why": "This is the one step nobody can do for you — it is your account "
                "and your login."},
        {"key": "sync", "n": 4, "label": "Ask Robinhood which coins it will trade",
         "done": confirmed > 0, "blocked": reachable is not True,
         "detail": (f"{confirmed} coins confirmed tradable" if confirmed
                    else "never asked — all 50 coins are unverified"),
         "button": "sync",
         "why": "is_api_tradable is stricter than 'visible in the app'. Until this "
                "runs, the system may be planning trades Robinhood would refuse."},
        {"key": "spreads", "n": 5, "label": "Measure the real spread per coin",
         "done": measured > 1, "blocked": reachable is not True,
         "detail": (f"{measured} coins measured" if measured
                    else "using the 0.95% default for every coin"),
         "button": "spreads",
         "why": "Cost is the largest term in whether any of this is profitable. "
                "Right now only DOGE's spread was actually read off a ticket."},
    ]
    done = sum(1 for s in steps if s["done"])
    return {
        "steps": steps, "done": done, "total": len(steps),
        "complete": done == len(steps),
        "api_key_hint": api_key_hint,
        "credentials_file": str(credentials_path()),
        "account": account,
        "python": _venv_python(),
    }


def install(package: str) -> dict:
    spec = INSTALLABLE.get(package.lower())
    if not spec:
        return {"error": f"{package!r} is not installable from here",
                "allowed": sorted(INSTALLABLE)}
    if _installed(spec["import_name"]):
        return {"ok": True, "already": True, "package": package}
    try:
        proc = subprocess.run(
            [_venv_python(), "-m", "pip", "install", spec["spec"]],
            capture_output=True, text=True, timeout=300, cwd=str(BACKEND))
    except subprocess.TimeoutExpired:
        return {"error": "pip timed out after 5 minutes"}
    out = (proc.stdout + proc.stderr)[-4000:]
    ok = proc.returncode == 0
    db.log_event("INFO" if ok else "ERROR", "setup",
                 f"pip install {spec['spec']}: {'ok' if ok else 'FAILED'}")
    return {"ok": ok, "package": package, "output": out,
            "restart_required": ok,
            "note": ("Installed. The app has to restart before it can use it — "
                     "press Restart, and it comes back on its own in a few seconds."
                     if ok else "pip failed; the output above says why.")}


def generate_keypair() -> dict:
    """Make an Ed25519 pair. The private half is written straight to disk and
    never returned — not to the browser, not to a log, not to Claude."""
    try:
        import nacl.signing
    except ImportError:
        return {"error": "PyNaCl is not installed yet — do step 1 first."}
    sk = nacl.signing.SigningKey.generate()
    priv = base64.b64encode(sk.encode()).decode()
    pub = base64.b64encode(sk.verify_key.encode()).decode()
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except Exception:
            existing = {}
    existing["private_key"] = priv
    existing.setdefault("api_key", "")
    path.write_text(json.dumps(existing, indent=2))
    os.chmod(path, 0o600)
    db.log_event("INFO", "setup", "generated a new Ed25519 key pair for Robinhood")
    return {
        "public_key": pub,
        "saved_to": str(path),
        "next": ("Copy the public key above. On Robinhood web classic go to crypto "
                 "account settings, choose Add key, paste it there, and Robinhood "
                 "gives you an API key. Paste that back here."),
        "note": "The private half was written to disk with 0600 permissions and is "
                "not shown here on purpose.",
    }


def save_api_key(api_key: str) -> dict:
    api_key = (api_key or "").strip()
    if not api_key:
        return {"error": "paste the API key Robinhood gave you"}
    path = credentials_path()
    if not path.exists():
        return {"error": "generate a key pair first (step 2)"}
    d = json.loads(path.read_text())
    if not d.get("private_key"):
        return {"error": "no private key on file — generate a key pair first"}
    d["api_key"] = api_key
    path.write_text(json.dumps(d, indent=2))
    os.chmod(path, 0o600)
    db.log_event("INFO", "setup", "Robinhood API key saved")
    return {"ok": False, "probe": {"reachable": None,
            "error": "no broker integration in this build — the order-placing and account code was removed when this repository was published"}}


def restart() -> dict:
    """Exit with the PLANNED-RESTART code, so the supervisor brings it straight back.

    2026-09-22: this exited with `health.MEMORY_EXIT_CODE`. That code was split
    from the planned-restart code on 2026-09-19 precisely because one number was
    doing two jobs -- so every press of the dashboard's Restart button was
    recorded by the supervisor as "memory-ceiling restart (planned)" and paid the
    5-second backoff that a memory event deserves and a deliberate restart does
    not. It worked; it lied about why, in the log the operator reads when
    something has gone wrong, and it was slower than it needed to be.
    """
    import threading
    import time as _t
    from app.core import autoapply

    def _bye():
        _t.sleep(0.6)
        try:
            db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        os._exit(autoapply.RESTART_EXIT_CODE)

    supervised = shutil.which("launchctl") is not None
    db.log_event("INFO", "setup", "restart requested from the dashboard")
    threading.Thread(target=_bye, daemon=True).start()
    return {"restarting": True, "supervised": supervised,
            "note": ("Back in a few seconds. The page will reconnect on its own."
                     if supervised else
                     "If nothing is supervising this process it will not come back "
                     "by itself.")}


def restore_database() -> dict:
    """Swap in data/tradecrypto.recovered.sqlite and restart.

    Done here rather than in a terminal because the app is the only thing that
    knows when it is safe: it renames the files and then immediately exits, so
    nothing is holding the old file open when the supervisor brings it back.
    """
    import threading
    import time as _t
    from app.core import health

    s = get_settings()
    live = s.db_file
    cand = live.with_name("tradecrypto.recovered.sqlite")
    if not cand.exists():
        return {"error": f"no recovered database at {cand}"}

    # Verify the candidate BEFORE touching anything that works.
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{cand}?mode=ro", uri=True)
        verdict = c.execute("PRAGMA integrity_check").fetchone()[0]
        bars = c.execute("SELECT COUNT(*) FROM bars").fetchone()[0]
        c.close()
    except Exception as exc:
        return {"error": f"the recovered file is not readable: {exc}"}
    if verdict != "ok":
        return {"error": f"the recovered file fails its own integrity check: {verdict}"}

    stamp = _t.strftime("%Y%m%d-%H%M%S")
    broken = live.with_name(f"tradecrypto.corrupt-{stamp}.sqlite")

    def _swap():
        _t.sleep(0.5)
        moved_aside = False
        try:
            for suffix in ("-wal", "-shm"):
                p = live.with_name(live.name + suffix)
                if p.exists():
                    p.rename(p.with_name(p.name + f".old-{stamp}"))
            live.rename(broken)
            moved_aside = True
            cand.rename(live)
        except Exception as exc:
            # The dangerous case is a HALF-completed swap: the live database has
            # been renamed aside and the replacement did not land. The old code
            # swallowed that and exited with the planned-restart code, so the
            # supervisor restarted cheerfully and sqlite created a brand new empty
            # database. The real data survived under another name with nothing
            # saying so. Put it back, say what happened, and do not restart.
            try:
                if moved_aside and broken.exists() and not live.exists():
                    broken.rename(live)
            except Exception:
                pass
            try:
                db.log_event("ERROR", "system",
                             f"database restore FAILED and was rolled back: "
                             f"{type(exc).__name__}: {exc}")
            except Exception:
                pass
            os._exit(1)
        os._exit(health.MEMORY_EXIT_CODE)

    db.log_event("WARN", "system",
                 f"restoring recovered database ({bars:,} bars); old file kept as {broken.name}")
    threading.Thread(target=_swap, daemon=True).start()
    return {"restoring": True, "bars": bars, "integrity": verdict,
            "old_file_kept_as": broken.name,
            "note": "Swapping now and restarting. Reload in about ten seconds."}


def fix_text_encoding() -> dict:
    """Convert TEXT columns that came back as BLOBs during the salvage.

    The recovery script read the damaged database with `text_factory = bytes`
    so that undecodable bytes on the corrupt page could not abort the read. That
    worked, and then wrote every TEXT value back as a BLOB — so `symbol` became
    b'TIA' rather than 'TIA', and every lookup by symbol silently matched
    nothing. The engine ticked, fetched quotes for b'WLD', and got 404s.

    CAST(col AS TEXT) converts them in place. Run by the app itself, because
    writing to this file from anywhere else is what corrupted it in the first
    place.
    """
    conn = db.get_conn()
    fixed, failed, scanned = [], [], 0
    tables = [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = db.query(f"PRAGMA table_info(`{t}`)")
        for c in cols:
            name, decl = c["name"], (c["type"] or "").upper()
            if "CHAR" not in decl and "TEXT" not in decl and "CLOB" not in decl:
                continue
            scanned += 1
            row = db.query_one(
                f"SELECT COUNT(*) n FROM `{t}` WHERE typeof(`{name}`)='blob'")
            n = row["n"] if row else 0
            if not n:
                continue
            # One column must not be able to stop the rest. The first run died
            # on model_cards, where converting a blob produced a row identical
            # to one already stored as text and tripped the unique index --
            # leaving every table after it in the loop untouched.
            try:
                conn.execute(
                    f"UPDATE `{t}` SET `{name}` = CAST(`{name}` AS TEXT) "
                    f"WHERE typeof(`{name}`)='blob'")
                conn.commit()
                fixed.append({"table": t, "column": name, "rows": n})
            except Exception as exc:
                conn.rollback()
                if "UNIQUE" not in str(exc).upper():
                    failed.append({"table": t, "column": name,
                                   "error": f"{type(exc).__name__}: {exc}"})
                    continue
                # The blob row duplicates a text row that already exists, so the
                # blob is the stale copy. Drop it and convert what remains.
                try:
                    conn.execute(f"DELETE FROM `{t}` WHERE typeof(`{name}`)='blob'")
                    conn.commit()
                    fixed.append({"table": t, "column": name, "rows": n,
                                  "note": "duplicate blob rows removed"})
                except Exception as exc2:
                    conn.rollback()
                    failed.append({"table": t, "column": name,
                                   "error": f"{type(exc2).__name__}: {exc2}"})
    conn.commit()
    total = sum(f["rows"] for f in fixed)
    db.log_event("INFO" if fixed else "INFO", "system",
                 f"text encoding fix: {total:,} values converted from blob to text "
                 f"across {len(fixed)} columns")
    return {"columns_scanned": scanned, "columns_fixed": len(fixed),
            "values_converted": total, "detail": fixed, "failed": failed,
            "restart_required": bool(fixed),
            "note": ("Converted. Restart so nothing is holding a stale cached value."
                     if fixed else "Nothing needed converting.")}


PAPER_BOOK_TABLES = ("trades", "orders", "positions", "signals", "equity_curve",
                     "trade_budgets", "strategy_state")


def reset_paper_book(mode: str = "paper") -> dict:
    """Wipe the paper trading record and start clean.

    Clears ONLY the book: trades, orders, positions, signals, the equity curve,
    budgets and the strategy posteriors built from them. Everything that took
    real time to collect is untouched -- price bars, the universe, measured
    Robinhood spreads, model cards, news, research papers.

    Why this exists: the first paper fills were $25 positions from a
    twenty-minute scalper, held for eighteen seconds. Leaving them in the record
    poisons every number that reads from it -- the posteriors, the allocation
    fractions, the win rate on the Overview page -- with results from a strategy
    that has since been retired. A clean book means the next sample measures the
    strategy actually running.
    """
    before = {}
    for t in PAPER_BOOK_TABLES:
        try:
            row = db.query_one(f"SELECT COUNT(*) c FROM {t}")
            before[t] = int(row["c"]) if row else 0
        except Exception:
            before[t] = 0

    cleared, kept = {}, {}
    for t in PAPER_BOOK_TABLES:
        try:
            if t in ("trades", "orders", "positions", "equity_curve"):
                db.execute(f"DELETE FROM {t} WHERE mode=?", (mode,))
            elif t == "trade_budgets":
                db.execute("DELETE FROM trade_budgets WHERE mode=?", (mode,))
            else:                       # signals and strategy_state have no mode column
                db.execute(f"DELETE FROM {t}")
            cleared[t] = before[t]
        except Exception as exc:
            cleared[t] = f"failed: {type(exc).__name__}: {exc}"

    # The kill switch is a FILE, not a table. Clearing the book left it in place,
    # so a cap breached by trades that no longer exist went on blocking every
    # order the next morning. Clearing the book clears the switch with it.
    try:
        ks = get_settings().kill_switch_file
        if ks.exists():
            ks.unlink()
            cleared["kill_switch"] = "released"
    except Exception as exc:
        cleared["kill_switch"] = f"failed: {exc}"

    for t in ("bars", "universe", "rh_spreads", "model_cards", "news", "papers"):
        try:
            row = db.query_one(f"SELECT COUNT(*) c FROM {t}")
            kept[t] = int(row["c"]) if row else 0
        except Exception:
            pass

    db.log_event("WARNING", "setup",
                 f"paper book reset in {mode} mode: " +
                 ", ".join(f"{k}={v}" for k, v in cleared.items() if isinstance(v, int) and v))
    return {"reset": True, "mode": mode, "cleared": cleared, "kept": kept,
            "note": ("Book cleared. Price history, the universe, measured spreads and "
                     "model cards are untouched. The next fills are the sample.")}


RESET_MARKER = "paper_book_reset_2026_09_09b"   # bumped: clear the two xs_momentum losses


RESHAPE_KEY = "reports_reclassified_2026_09_17"


def reclassify_reports_once() -> dict:
    """Rebuild stored daily reports so each miss carries its shape.

    Reports written before the shape classifier existed have no `shape` field, so
    the shape memory would only ever see an "unknown" bucket — 209 coin-days of
    it, which is enough to cross any threshold while carrying no information.
    """
    try:
        if db.query_one("SELECT value FROM app_state WHERE key=?", (RESHAPE_KEY,)):
            return {"rebuilt": False, "reason": "already done"}
        from app.research import daily_report as _dr
        days = [r["day"] for r in db.query("SELECT day FROM daily_reports ORDER BY day")]
        done = 0
        for d in days:
            try:
                _dr.get(d, force=True)
                done += 1
            except Exception:
                continue
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (RESHAPE_KEY, "1", time.time()))
        db.log_event("INFO", "setup", f"reclassified {done} daily report(s) by shape")
        return {"rebuilt": True, "days": done}
    except Exception as exc:
        return {"rebuilt": False, "error": f"{type(exc).__name__}: {exc}"}




SENTINEL_TARGET_KEY = "sentinel_targets_cleared_2026_09_18"


def clear_sentinel_targets_once() -> dict:
    """Erase stored exit targets that were never prices.

    pump_ride said "no profit target" with target_bps = 100_000, and the engine
    wrote it out as a real one: an ARB position bought at $0.2245 carried a
    target of $2.4692. Nothing is recoverable here and nothing needs to be — the
    target never existed, so the honest value is NULL. The position's stop and
    its trail are untouched, because those are real and are what actually exits
    the trade.

    Anything more than 3x or less than a third of what was paid is treated as a
    sentinel. A genuine target that far away would itself be a bug.
    """
    try:
        if db.query_one("SELECT value FROM app_state WHERE key=?", (SENTINEL_TARGET_KEY,)):
            return {"cleared": 0, "reason": "already done"}
        rows = db.query("SELECT symbol, mode, avg_px, target_px FROM positions "
                        "WHERE target_px IS NOT NULL AND qty > 0")
        cleared = []
        for r in rows:
            entry, target = float(r["avg_px"] or 0), float(r["target_px"] or 0)
            if entry > 0 and target > 0 and not (0.33 < target / entry < 3.0):
                db.execute("UPDATE positions SET target_px=NULL WHERE symbol=? AND mode=?",
                           (r["symbol"], r["mode"]))
                cleared.append(f"{r['symbol']} ({target / entry:.1f}x)")
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (SENTINEL_TARGET_KEY, "1", time.time()))
        if cleared:
            db.log_event("WARNING", "setup",
                         f"cleared {len(cleared)} impossible exit target(s): "
                         + ", ".join(cleared)
                         + ". These were sentinels meaning 'no target', stored as prices.")
        return {"cleared": len(cleared), "which": cleared}
    except Exception as exc:
        return {"cleared": 0, "error": f"{type(exc).__name__}: {exc}"}


BACKFILL_LINK_KEY = "trade_order_linkage_backfilled_2026_09_17"


def backfill_trade_links_once() -> dict:
    """Recover the order linkage on trades written before the fix.

    Every trade on the book had NULL open_order_id and close_order_id, so none
    could be joined back to the features that caused them. Orders carry symbol,
    side, mode and a fill timestamp, and the engine stamps a trade's ts_open and
    ts_close from those same fills, so the match is exact rather than inferred.
    A trade whose order cannot be identified inside the tolerance is left NULL
    and reported — a guessed linkage is worse than none.
    """
    TOLERANCE_S = 5.0
    try:
        if db.query_one("SELECT value FROM app_state WHERE key=?", (BACKFILL_LINK_KEY,)):
            return {"linked": 0, "reason": "already done"}
        rows = db.query(
            "SELECT id, symbol, mode, ts_open, ts_close FROM trades "
            "WHERE open_order_id IS NULL OR close_order_id IS NULL")
        linked = ambiguous = 0
        for t in rows:
            o = db.query_one(
                "SELECT id, ABS(ts_filled - ?) AS d FROM orders "
                "WHERE symbol=? AND mode=? AND side='buy' AND status='filled' "
                "  AND ts_filled IS NOT NULL ORDER BY d LIMIT 1",
                (t["ts_open"], t["symbol"], t["mode"]))
            c = db.query_one(
                "SELECT id, ABS(ts_filled - ?) AS d FROM orders "
                "WHERE symbol=? AND mode=? AND side='sell' AND status='filled' "
                "  AND ts_filled IS NOT NULL ORDER BY d LIMIT 1",
                (t["ts_close"], t["symbol"], t["mode"]))
            ok_o = o is not None and o["d"] is not None and float(o["d"]) <= TOLERANCE_S
            ok_c = c is not None and c["d"] is not None and float(c["d"]) <= TOLERANCE_S
            if ok_o and ok_c:
                db.execute("UPDATE trades SET open_order_id=?, close_order_id=? WHERE id=?",
                           (o["id"], c["id"], t["id"]))
                linked += 1
            else:
                ambiguous += 1

        # Positions still OPEN matter more than closed ones: when they close they
        # become trades, and an unlinked position produces another unlinkable
        # trade. Same matching, same refusal to guess.
        pos_linked = pos_ambiguous = 0
        for pos in db.query("SELECT symbol, mode, opened_ts FROM positions "
                            "WHERE qty > 0 AND open_order_id IS NULL"):
            o = db.query_one(
                "SELECT id, ABS(ts_filled - ?) AS d FROM orders "
                "WHERE symbol=? AND mode=? AND side='buy' AND status='filled' "
                "  AND ts_filled IS NOT NULL ORDER BY d LIMIT 1",
                (pos["opened_ts"], pos["symbol"], pos["mode"]))
            if o and o["d"] is not None and float(o["d"]) <= TOLERANCE_S:
                db.execute("UPDATE positions SET open_order_id=? WHERE symbol=? AND mode=?",
                           (o["id"], pos["symbol"], pos["mode"]))
                pos_linked += 1
            else:
                pos_ambiguous += 1

        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (BACKFILL_LINK_KEY, "1", time.time()))
        db.log_event("INFO", "setup",
                     f"backfilled order linkage on {linked} trade(s) and "
                     f"{pos_linked} open position(s); {ambiguous + pos_ambiguous} "
                     f"could not be matched within {TOLERANCE_S:.0f}s and were left "
                     f"NULL rather than guessed")
        return {"linked": linked, "ambiguous": ambiguous,
                "positions_linked": pos_linked, "positions_ambiguous": pos_ambiguous,
                "tolerance_s": TOLERANCE_S}
    except Exception as exc:
        return {"linked": 0, "error": f"{type(exc).__name__}: {exc}"}


def reconcile_declared_stake() -> dict:
    """Make the book hold the capital the operator declared.

    `TC_ACCOUNT_EQUITY` in the .env is only a FALLBACK. The real declared stake
    lives in `app_state.account_equity`, written by the Setup page, and it wins.
    So raising the .env to 2000 changed nothing: the stake stayed at the $498 set
    on 2026-09-08 and the book kept sizing off ~$190 of free cash.

    Setting the stake is the whole job. The engine marks the book every tick as

        equity = declared_stake + realised_pnl + unrealised_pnl

    so raising the stake raises equity on the very next tick, and free cash with
    it. An earlier version of this function also wrote a "deposit" row into the
    equity curve; that row was overwritten by the next mark a few seconds later,
    because equity is derived from the stake rather than accumulated. It is gone.

    Realised P&L is untouched either way, so the trading record still says exactly
    what the strategies earned. Adding capital is not a profit and is never shown
    as one.
    """
    try:
        from app.core import mode as mode_mod
        target = float(get_settings().account_equity)
        declared = mode_mod.get_equity()
        if target <= declared:
            return {"raised": False, "declared": declared,
                    "reason": f"declared stake ${declared:,.2f} already meets "
                              f"TC_ACCOUNT_EQUITY ${target:,.2f}"}
        mode_mod.set_equity(target, note="(raised to match TC_ACCOUNT_EQUITY)")
        row = db.query_one(
            "SELECT realised_pnl, unrealised_pnl FROM equity_curve "
            "WHERE mode='paper' ORDER BY ts DESC LIMIT 1")
        realised = float((row["realised_pnl"] if row else 0.0) or 0.0)
        db.log_event(
            "INFO", "account",
            f"declared stake raised ${declared:,.2f} -> ${target:,.2f}. Equity "
            f"follows on the next mark; realised P&L stays at ${realised:,.2f} "
            f"— adding capital is not a profit")
        return {"raised": True, "from": declared, "to": target,
                "deposited": target - declared, "realised_pnl": realised}
    except Exception as exc:
        return {"raised": False, "error": f"{type(exc).__name__}: {exc}"}


REPAIR_KEY = "pnl_double_count_repaired_2026_09_13"
REATTRIBUTE_KEY = "verdicts_reattributed_2026_09_16"


def reattribute_trades_once() -> dict:
    """Recompute every stored trade's verdict after the labelling fix.

    Existing rows carry "model wrong" on every single trade, profitable ones
    included, because the old test compared against a prediction that was never
    made. The rows are fine; only the label was wrong, so it is recomputed in
    place rather than asking the operator to wait for new trades.
    """
    try:
        if db.query_one("SELECT value FROM app_state WHERE key=?", (REATTRIBUTE_KEY,)):
            return {"reattributed": False, "reason": "already done"}
        from app.feedback import loop as _loop
        ids = [r["id"] for r in db.query("SELECT id FROM trades")]
        done = 0
        for tid in ids:
            try:
                _loop.attribute_trade(tid)
                done += 1
            except Exception:
                continue
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (REATTRIBUTE_KEY, "1", time.time()))
        if done:
            db.log_event("INFO", "setup", f"re-labelled {done} trade verdict(s)")
        return {"reattributed": True, "trades": done}
    except Exception as exc:
        return {"reattributed": False, "error": f"{type(exc).__name__}: {exc}"}


def repair_double_counted_pnl_once() -> dict:
    """Re-book trades that were charged the spread twice, once.

    `_close_position` computed gross from FILL prices -- which already contain
    both sides of the spread -- and then subtracted the spread again. Every
    closed trade before 2026-09-13 therefore reads roughly 2.6x worse than it
    was. The live code is fixed; these rows are not, and they feed realised P&L,
    the equity curve and every posterior.

    The repair needs no re-reading of orders. The stored columns already hold
    what is needed:

        gross_pnl_usd  was actually fill-to-fill  -> the TRUE net
        cost_usd       was computed correctly     -> stays
        net_pnl_usd    was gross - cost           -> the double count

    so true_gross = true_net + cost, and the three become consistent again.
    """
    try:
        if db.query_one("SELECT value FROM app_state WHERE key=?", (REPAIR_KEY,)):
            return {"repaired": False, "reason": "already done"}
        rows = db.query("SELECT id, gross_pnl_usd, cost_usd, net_pnl_usd FROM trades")
        fixed, delta = 0, 0.0
        for r in rows:
            true_net = float(r["gross_pnl_usd"] or 0.0)
            cost = float(r["cost_usd"] or 0.0)
            if abs(float(r["net_pnl_usd"] or 0.0) - true_net) < 1e-9:
                continue                       # already consistent
            db.execute(
                "UPDATE trades SET gross_pnl_usd=?, net_pnl_usd=? WHERE id=?",
                (true_net + cost, true_net, r["id"]))
            delta += true_net - float(r["net_pnl_usd"] or 0.0)
            fixed += 1
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (REPAIR_KEY, "1", time.time()))
        if fixed:
            db.log_event("INFO", "setup",
                         f"re-booked {fixed} trade(s) that were charged the spread twice; "
                         f"realised P&L moves {delta:+.2f}")
        return {"repaired": True, "trades_fixed": fixed, "pnl_delta_usd": round(delta, 2)}
    except Exception as exc:
        return {"repaired": False, "error": f"{type(exc).__name__}: {exc}"}


def reset_paper_book_once() -> dict:
    """Clear the book a single time on boot, then never again.

    The book still held results from strategies that have since been retired --
    $25 positions from a twenty-minute scalper, held for eighteen seconds. Those
    rows feed the posteriors, the allocation fractions and every win rate on the
    dashboard, so leaving them in means the next month of numbers is measuring a
    strategy that no longer exists.

    This runs itself rather than living as a button, because it is needed exactly
    once and a permanent "delete everything" control is a hazard, not a feature.
    """
    try:
        row = db.query_one("SELECT value FROM app_state WHERE key=?", (RESET_MARKER,))
        if row:
            return {"reset": False, "reason": "already done"}
    except Exception:
        return {"reset": False, "reason": "app_state unavailable"}

    import time as _t
    out = reset_paper_book("paper")
    try:
        db.execute(
            "INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (RESET_MARKER, "1", _t.time()))
    except Exception:
        pass
    return out



# ── recovery from the 2026-09-18 corruption ──────────────────────────────────

RECOVERED_TRADES_KEY = "lost_trades_restored_2026_09_17"
ID_HIGH_WATER_KEY = "trades_max_id_ever"


def enforce_trade_id_high_water_mark() -> dict:
    """Never hand out a trade id that has been used before, even after a restore.

    THE BUG THIS EXISTS FOR. Restoring the database from a backup rolls
    `sqlite_sequence` back with it. On 2026-09-18 the backup's counter said 31,
    so the next four trades took ids 32, 33, 34 and 35 -- the ids that four real
    trades from 2026-09-17 already had. Nothing errored. Trade #33 was simply a
    UNI trade one day and an ARB trade the next, and every reference to the old
    row silently pointed at the new one.

    The mark lives in app_state, which travels with the database, so a restore
    carries the high-water mark forward even when the trades themselves are gone.
    Runs every boot: cheap, and the failure it prevents is silent.
    """
    try:
        from app.core import mode as _mode
        _mode._ensure()                      # app_state lives there, not in SCHEMA
        row = db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM trades")
        here = int(row["m"] if row else 0)
        rec = db.query_one("SELECT value FROM app_state WHERE key=?", (ID_HIGH_WATER_KEY,))
        ever = int(float(rec["value"])) if rec and rec["value"] is not None else 0
        mark = max(here, ever)
        if mark > here:
            # The database has been rolled back beneath ids that were already
            # issued. Push the counter past them so none is ever reused.
            db.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES ('trades', 0)")
            db.execute("UPDATE sqlite_sequence SET seq=? WHERE name='trades'", (mark,))
            db.log_event("WARNING", "system",
                         f"trade ids were rolled back to {here} but {mark} had already "
                         f"been issued; counter pushed to {mark} so none is reused")
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                   "updated_ts=excluded.updated_ts",
                   (ID_HIGH_WATER_KEY, str(mark), time.time()))
        return {"max_now": here, "high_water": mark, "bumped": mark > here}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def restore_lost_trades_once() -> dict:
    """Put back the trades the 2026-09-18 restore destroyed, and correct the one it faked.

    Three trades that closed on 2026-09-17 (UNI, NEAR, ONDO) were lost when the
    database was restored from an eighteen-hour-old backup. A fourth, ZEC, was
    worse than lost: it had really closed at 09:15 on the 17th for $9.03, but the
    backup still showed the position open, so the engine closed it again at the
    next boot, at the next day's price, and recorded $24.56 on the wrong day.
    A wrong trade is worse than a missing one -- it feeds P&L, the posteriors,
    sizing and the exit study.

    All four were salvaged from the raw B-tree pages of the corrupt file and live
    in data/recovered-2026-09-17.json. Matching is on (symbol, strategy, ts_open),
    never on id, because the ids were reused by different trades.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        from app.core import mode as _mode
        _mode._ensure()
        if db.query_one("SELECT value FROM app_state WHERE key=?", (RECOVERED_TRADES_KEY,)):
            return {"restored": 0, "reason": "already done"}

        src = _Path(get_settings().db_file).parent / "recovered-2026-09-17.json"
        if not src.exists():
            return {"restored": 0, "reason": "no recovery file"}
        payload = _json.loads(src.read_text())

        restored, corrected = [], []
        for t in payload.get("trades", []):
            match = db.query_one(
                "SELECT id, ts_close, net_pnl_usd FROM trades "
                "WHERE symbol=? AND strategy=? AND ABS(ts_open-?) < 90",
                (t["symbol"], t["strategy"], float(t["ts_open"])))
            # holding_s, gross and cost are NOT NULL. A salvaged row can be
            # missing them, and a recovery that fails on a constraint recovers
            # nothing — so derive what is derivable rather than passing NULL.
            hold = t.get("holding_s")
            if hold is None:
                hold = float(t["ts_close"]) - float(t["ts_open"])
            cost = t.get("cost")
            cost = 0.0 if cost is None else float(cost)
            gross = t.get("gross")
            gross = float(t["net"]) + cost if gross is None else float(gross)

            if match is None:
                db.execute(
                    "INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, "
                    "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, "
                    "attribution_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["symbol"], t["strategy"], t.get("mode", "paper"), t["qty"],
                     t["entry_px"], t["exit_px"], t["ts_open"], t["ts_close"],
                     hold, gross, cost, t["net"],
                     _json.dumps({"provenance": "salvaged_from_corrupt_pages_2026_09_18"})))
                restored.append(f"{t['symbol']} {t['net']:+.2f}")
            elif abs(float(match["ts_close"] or 0) - float(t["ts_close"])) > 90:
                # Same entry, a close that never happened. Replace it with the real one.
                db.execute(
                    "UPDATE trades SET exit_px=?, ts_close=?, holding_s=?, gross_pnl_usd=?, "
                    "cost_usd=?, net_pnl_usd=?, attribution_json=? WHERE id=?",
                    (t["exit_px"], t["ts_close"], hold, gross, cost, t["net"],
                     _json.dumps({"provenance": "corrected_from_corrupt_pages_2026_09_18",
                                  "replaced_fabricated_close": match["ts_close"],
                                  "replaced_net": match["net_pnl_usd"]}),
                     match["id"]))
                corrected.append(f"{t['symbol']} {match['net_pnl_usd']:+.2f} -> {t['net']:+.2f}")

        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?, '1', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value='1', updated_ts=excluded.updated_ts",
                   (RECOVERED_TRADES_KEY, time.time()))
        if restored or corrected:
            db.log_event("WARNING", "system",
                         f"recovered {len(restored)} lost trade(s) and corrected "
                         f"{len(corrected)} fabricated close(s) from 2026-09-17")
        return {"restored": len(restored), "which": restored,
                "corrected": len(corrected), "fixed": corrected}
    except Exception as exc:
        return {"restored": 0, "error": f"{type(exc).__name__}: {exc}"}


DATA_GAPS_KEY = "data_gaps_declared_v1"


def declare_data_gaps_once() -> dict:
    """Record the windows where this database is known to be incomplete.

    A restored ledger looks exactly like a ledger where nothing happened. On
    2026-09-18 the operator went looking for a $1,132 ARB position he clearly
    remembered and found nothing — not because the app was hiding it, but
    because the restore predated it and nothing anywhere said so.

    The gap is therefore data, not a footnote in a chat log: it is loaded from
    data/data-gaps.json into a table the UI and the invariants can read, so the
    journal shows a declared hole rather than a quiet absence.

    It is NEVER filled by re-creating the missing positions. A position from
    twenty hours ago, closed at today's price, is a fabricated trade — that is
    exactly how ZEC came to be recorded on the wrong day for three times its
    real profit.
    """
    import json as _json
    from pathlib import Path as _Path
    try:
        from app.core import mode as _mode
        _mode._ensure()
        db.execute("""CREATE TABLE IF NOT EXISTS data_gaps (
            id TEXT PRIMARY KEY,
            from_ts REAL NOT NULL, to_ts REAL NOT NULL,
            from_label TEXT, to_label TEXT,
            cause TEXT NOT NULL,
            recovered_json TEXT, known_lost_json TEXT,
            unrecoverable INTEGER NOT NULL DEFAULT 1)""")

        src = _Path(get_settings().db_file).parent / "data-gaps.json"
        if not src.exists():
            return {"declared": 0, "reason": "no data-gaps.json"}
        gaps = _json.loads(src.read_text()).get("gaps", [])
        for g in gaps:
            db.execute(
                "INSERT INTO data_gaps(id, from_ts, to_ts, from_label, to_label, cause, "
                "recovered_json, known_lost_json, unrecoverable) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET cause=excluded.cause, "
                "recovered_json=excluded.recovered_json, known_lost_json=excluded.known_lost_json",
                (g["id"], float(g["from_ts"]), float(g["to_ts"]), g.get("from_label"),
                 g.get("to_label"), g["cause"], _json.dumps(g.get("recovered", [])),
                 _json.dumps(g.get("known_lost", [])), 1 if g.get("unrecoverable") else 0))
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?, '1', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value='1', updated_ts=excluded.updated_ts",
                   (DATA_GAPS_KEY, time.time()))
        return {"declared": len(gaps)}
    except Exception as exc:
        return {"declared": 0, "error": f"{type(exc).__name__}: {exc}"}
