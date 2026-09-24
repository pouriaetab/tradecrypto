"""Has this mechanism ever actually run?

THE PROBLEM THIS EXISTS FOR
---------------------------
Two failures that look different and are the same failure:

  * "you confirmed the trailing stop had moved several times, and now the app
     has no memory of it."  The ratchet code was real. It was gated on
     `trail_bps`, every strategy but one left that at 0, so the block was never
     entered -- not once, for any position, ever. The claim was made from
     reading the code instead of from evidence that the code had run.

  * "you tell me a strategy is in place, and days later we find out it was
     built but never went live."  Same shape: shipped, plausible, never
     executed.

Both are invisible to everything we already have. Tests pass -- they call the
function directly. The gate passes -- the code is present and correct. The UI
shows a setting that is on. Nothing anywhere asks the only question that would
have caught it: **did this ever fire in production?**

WHAT THIS DOES
--------------
Every mechanism that matters calls `fired("name")` when it does its job. That is
one indexed upsert; it is not a log line, because logs rotate and nobody greps
them. Then:

  * `report()` lists every registered mechanism and when it last fired.
  * a mechanism that has NEVER fired, past its grace period, is a FAILURE --
    surfaced by the gate and on the dashboard, in red, by name.

The point is not the counter. The point is that "built" and "running" stop
being the same word.

WHAT IT DELIBERATELY IS NOT
---------------------------
Not a metrics system, not a tracer, not sampled. It answers one question -- has
this ever happened, and when was the last time -- because that is the question
that was being got wrong. Anything more would be a second system to maintain
and a second thing that can silently stop working.
"""
from __future__ import annotations

import time

from app.core import db

# name -> (what it is, how long before "never fired" is alarming, why)
#
# `grace_s` is not a guess about how often the thing SHOULD happen. It is how
# long we are willing to not know. Past it, silence becomes a finding.
REGISTRY: dict[str, dict] = {
    "strategy_control": {
        "what": "the operator switched a strategy off, or set a budget for it, in the app",
        "grace_s": 30 * 24 * 3600,
        "why": "Added 2026-09-21 after burst_catch lost $14.39 in three hours and "
               "the operator had no way to stop it without an engineer. A switch "
               "nobody has ever thrown is a switch nobody has ever proved works. "
               "This fires on the SET, not on the refusal, so it tells us the "
               "control surface is reachable -- not that a strategy is off.",
        "known_not_live": "2026-09-21: built today; fires the first time a strategy "
                          "is switched off or given a budget from the Strategies tab.",
    },
    "stop_ratchet": {
        "what": "a position's stop was raised behind a rising price",
        "grace_s": 24 * 3600,
        "why": "The thing the operator was told was happening for weeks while "
               "trail_bps was 0 on every position and the block was never entered.",
        # DECLARED not-live, with a reason and a date. Same principle as
        # data_gaps: an exception nobody can see is indistinguishable from the
        # bug the check exists to catch (blueprint 4.2), and a gate that is
        # permanently red gets switched off (5.1). Delete this line the day a
        # strategy sets trail_bps, and the check goes back to being a hard
        # failure if it still never fires.
        "known_not_live": "2026-09-19: no strategy sets trail_bps, so the ratchet "
                          "is unreachable by design right now. The exit lab has it "
                          "measured (+0.34 pts/trade over its own control on 32 "
                          "trades) and the lab's own bar is 40 trades.",
    },
    "stop_locked_breakeven": {
        "what": "a stop reached the point where the trade can no longer lose",
        "grace_s": 7 * 24 * 3600,
        "why": "Promised on a live ARB position on 2026-09-17. It has fired zero "
               "times in the entire history of this desk.",
        "known_not_live": "2026-09-19: downstream of stop_ratchet — it cannot fire "
                          "until a stop is trailing in the first place.",
    },
    "trade_closed": {
        "what": "a position was closed and written to the ledger",
        "grace_s": 48 * 3600,
        "why": "If this goes quiet the desk has stopped trading, whatever the "
               "dashboard says.",
    },
    "order_placed": {
        "what": "an order was sent to a broker",
        "grace_s": 48 * 3600,
        "why": "Same, one step earlier.",
    },
    "vault_write": {
        "what": "a record was appended to the write-once vault",
        "grace_s": 48 * 3600,
        "why": "A vault nothing writes to is a folder.",
    },
    "auto_apply_restart": {
        "what": "the desk restarted itself to pick up new code",
        "grace_s": 14 * 24 * 3600,
        "why": "If this stops, every change is silently running against the old "
               "build -- which has already happened twice.",
    },
    "daily_report_rebuilt": {
        "what": "a finished day's report was rebuilt after a late trade",
        "grace_s": 30 * 24 * 3600,
        "why": "The Daily tab disagreed with the Journal for two days because "
               "nothing rebuilt a report once the day was over.",
    },
    "verdict_backfilled": {
        "what": "a trade with no verdict was given one",
        "grace_s": 30 * 24 * 3600,
        "why": "The one-shot that was supposed to do this had already retired "
               "before the rows that needed it existed.",
    },
}


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS mechanism_fires (
        name      TEXT PRIMARY KEY,
        n         INTEGER NOT NULL DEFAULT 0,
        first_ts  REAL,
        last_ts   REAL,
        last_note TEXT)""")


def fired(name: str, note: str | None = None) -> None:
    """Record that a mechanism just did its job. Never raises, never blocks.

    Deliberately cheap and deliberately not conditional on the registry: an
    unregistered name still records, so instrumenting something new is one call
    and you can decide later whether its silence should be alarming.
    """
    try:
        now = time.time()
        ensure_schema()
        db.execute(
            "INSERT INTO mechanism_fires(name, n, first_ts, last_ts, last_note) "
            "VALUES (?,1,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "n = n + 1, last_ts = excluded.last_ts, last_note = excluded.last_note",
            (name, now, now, (note or "")[:200]))
    except Exception:
        pass            # a counter must never be able to stop the thing it counts


def seed_from_history() -> dict:
    """Give each counter the truth that is already in the database.

    Without this, every mechanism reads as "never fired" on the day the counters
    were added, and the one finding that matters -- the ratchet that genuinely
    has never run, not once, in the entire history of this desk -- would be
    buried in four false ones. A check whose first report is mostly noise is a
    check that gets ignored.

    Only ever raises a count to what the evidence supports; never invents one.
    """
    seeded = {}
    ensure_schema()
    EVIDENCE = {
        "order_placed": "SELECT COUNT(*) n, MIN(ts_decided) a, MAX(ts_decided) b FROM orders",
        "trade_closed": "SELECT COUNT(*) n, MIN(ts_close) a, MAX(ts_close) b "
                        "FROM trades WHERE ts_close IS NOT NULL",
    }
    for name, sql in EVIDENCE.items():
        try:
            r = db.query_one(sql)
            if not r or not r["n"]:
                continue
            cur = db.query_one("SELECT n FROM mechanism_fires WHERE name=?", (name,))
            if cur and int(cur["n"]) >= int(r["n"]):
                continue
            db.execute(
                "INSERT INTO mechanism_fires(name, n, first_ts, last_ts, last_note) "
                "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "n = MAX(n, excluded.n), "
                "first_ts = MIN(COALESCE(first_ts, excluded.first_ts), excluded.first_ts), "
                "last_ts = MAX(COALESCE(last_ts, 0), excluded.last_ts), "
                "last_note = excluded.last_note",
                (name, int(r["n"]), r["a"], r["b"], "seeded from the ledger"))
            seeded[name] = int(r["n"])
        except Exception:
            continue
    # The vault keeps its own count, outside this database, which is the point
    # of it -- so take the number from there rather than from here.
    try:
        from app.core import vault
        st = vault.status()
        if st.get("records"):
            r = db.query_one("SELECT n FROM mechanism_fires WHERE name=?", ("vault_write",))
            if not r or int(r["n"]) < int(st["records"]):
                db.execute(
                    "INSERT INTO mechanism_fires(name, n, first_ts, last_ts, last_note) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                    "n = MAX(n, excluded.n), last_ts = excluded.last_ts, "
                    "last_note = excluded.last_note",
                    ("vault_write", int(st["records"]), st.get("last_write_ts"),
                     st.get("last_write_ts") or time.time(), "seeded from the vault"))
                seeded["vault_write"] = int(st["records"])
    except Exception:
        pass
    return seeded


def report() -> dict:
    """Every registered mechanism, and whether it has ever actually happened."""
    # READ ONLY. This runs from the gate, which opens the live database with
    # immutable=1 -- so it must never try to create its own table. A checker
    # that needs write access to report is a checker that cannot run where it
    # is most needed.
    try:
        rows = {r["name"]: dict(r)
                for r in db.query("SELECT * FROM mechanism_fires")}
    except Exception:
        rows = {}          # table not created yet: every mechanism reads as never fired

    now = time.time()
    # How long this desk has been collecting at all -- a mechanism cannot be
    # blamed for not firing before there was anything to fire on.
    #
    # DEMO ORDERS MUST NOT AGE THE DESK. The first-run demo replays a month of
    # real history, so its orders are timestamped weeks back. Reading the oldest
    # order made a five-minute-old install look a month old, and every mechanism
    # that had not yet fired was immediately reported as overdue in red — the
    # first thing a new user saw was their own install accusing itself.
    try:
        r = db.query_one(
            "SELECT MIN(ts_decided) m FROM orders "
            "WHERE ticket_json IS NULL OR ticket_json NOT LIKE '%\"demo\": true%'")
        since = float(r["m"]) if r and r["m"] else now
    except Exception:
        since = now
    age = now - since

    out, never, stale, declared_dark = [], [], [], []
    for name, spec in REGISTRY.items():
        row = rows.get(name)
        n = int(row["n"]) if row else 0
        last = float(row["last_ts"]) if row and row["last_ts"] else None
        overdue = age > spec["grace_s"]
        declared = spec.get("known_not_live")
        item = {
            "name": name, "what": spec["what"], "why": spec["why"],
            "times": n,
            "last_ts": last,
            "last_age_s": (now - last) if last else None,
            "grace_s": spec["grace_s"],
            "never": n == 0,
            "known_not_live": declared,
            # NEVER FIRED, past its grace period, AND nobody has written down
            # that this is expected. A declared one is a known hole; an
            # undeclared one is the bug this whole file exists to catch.
            "alarming": n == 0 and overdue and not declared,
        }
        out.append(item)
        if item["alarming"]:
            never.append(name)
        elif n == 0 and overdue and declared:
            declared_dark.append(name)
        elif last and (now - last) > spec["grace_s"]:
            stale.append(name)

    out.sort(key=lambda x: (not x["alarming"], x["name"]))
    return {
        "mechanisms": out,
        "never_fired": never,
        "known_not_live": declared_dark,
        "went_quiet": stale,
        "desk_age_s": age,
        "ok": not never,
        "headline": (f"{len(never)} mechanism(s) have NEVER run and nobody said why: "
                     f"{', '.join(never)}" if never else
                     (f"all running, except {len(declared_dark)} known not live: "
                      f"{', '.join(declared_dark)}" if declared_dark else
                      "every mechanism has fired at least once")),
    }
