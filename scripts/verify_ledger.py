"""Cross-check every trade in the live ledger against every independent copy.

Written on 2026-09-18, after a restore destroyed a day of records and the
operator said, correctly: "I don't know if they are real or not anymore."

The answer to that cannot be reassurance. It has to be corroboration. Each of
these was written at a different time by a different process, so a row that
appears identically in more than one of them was not invented afterwards:

  * data/backups/*.sqlite        — nightly VACUUM snapshots
  * data/ledger/*.sqlite         — 10-minute snapshots of the irreplaceable tables
  * data/_to_delete/corrupt-*    — the destroyed file, read via raw page salvage
  * data/recovered-2026-09-17.json — rows salvaged from those pages

Verdict per trade:
  CORROBORATED  identical in at least one independent copy
  SALVAGED      recovered from the corrupt file's raw pages, provenance stamped
  CORRECTED     deliberately corrected; copies older than the fix hold the old value
  UNCORROBORATED  exists only in the live database (normal for anything newer
                  than the last snapshot — check the timestamp before worrying)
  CONFLICT      appears in a copy with different numbers  <- always investigate

Read-only. Opens every file immutable; touches nothing.

    python3 scripts/verify_ledger.py [--all]
"""
from __future__ import annotations

import glob
import json
import sqlite3
import sys
import datetime as dt
from pathlib import Path

try:
    import zoneinfo
    CT = zoneinfo.ZoneInfo("America/Chicago")
except Exception:
    CT = None

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KEY_TOL_S = 90          # same trade if the open time matches this closely
MONEY_TOL = 0.01


def _fmt(ts):
    if not ts:
        return "open"
    d = dt.datetime.fromtimestamp(float(ts), CT) if CT else dt.datetime.fromtimestamp(float(ts))
    return d.strftime("%m-%d %H:%M")


def _read(path):
    """Trades from any SQLite copy, read immutably so a live file is untouched."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=10)
        rows = list(c.execute("SELECT symbol, strategy, ts_open, ts_close, "
                              "qty, entry_px, exit_px, net_pnl_usd, "
                              "attribution_json FROM trades"))
        c.close()
    except sqlite3.Error:
        return []
    return [dict(zip(("symbol", "strategy", "ts_open", "ts_close",
                      "qty", "entry_px", "exit_px", "net", "attr"), r)) for r in rows]


def _key(t):
    return (t["symbol"], t["strategy"], round(float(t["ts_open"]) / KEY_TOL_S))


def _same(a, b):
    for f in ("ts_close", "qty", "entry_px", "exit_px", "net"):
        x, y = a.get(f), b.get(f)
        if x is None or y is None:
            if x != y:
                return False
            continue
        tol = KEY_TOL_S if f == "ts_close" else max(MONEY_TOL, abs(float(y)) * 1e-4)
        if abs(float(x) - float(y)) > tol:
            return False
    return True


def main(show_all: bool) -> int:
    live_path = DATA / "tradecrypto.sqlite"
    live = _read(live_path)
    if not live:
        # The live file is WAL and may be unreadable from another machine; the
        # newest 10-minute snapshot is the same data and always safe to read.
        snaps = sorted(glob.glob(str(DATA / "ledger" / "ledger-*.sqlite")))
        if snaps:
            live_path = Path(snaps[-1])
            live = _read(live_path)
    if not live:
        print("could not read the live ledger or any snapshot")
        return 2

    sources = {}
    for p in sorted(glob.glob(str(DATA / "backups" / "*.sqlite"))):
        sources[f"backup {Path(p).name[13:26]}"] = _read(p)
    for p in sorted(glob.glob(str(DATA / "ledger" / "ledger-*.sqlite")))[:-1][-6:]:
        sources[f"snapshot {Path(p).stem[7:]}"] = _read(p)

    salvaged = {}
    rec = DATA / "recovered-2026-09-17.json"
    if rec.exists():
        for t in json.loads(rec.read_text()).get("trades", []):
            salvaged[_key(t)] = t

    print(f"live ledger: {live_path.name}  ({len(live)} trades)")
    print(f"independent copies: {len(sources)}  ({', '.join(sources) or 'none'})")
    print(f"salvaged rows on file: {len(salvaged)}\n")

    tally = {"CORROBORATED": 0, "SALVAGED": 0, "CORRECTED": 0,
             "UNCORROBORATED": 0, "CONFLICT": 0}
    lines = []
    for t in sorted(live, key=lambda x: float(x["ts_open"])):
        k = _key(t)
        seen_in, conflict_in = [], []
        for name, rows in sources.items():
            for o in rows:
                if _key(o) != k:
                    continue
                (seen_in if _same(t, o) else conflict_in).append(name)
                break
        attr = t.get("attr") or ""
        if conflict_in and "corrected_from_corrupt_pages" in attr:
            # A row we deliberately corrected will differ from every copy taken
            # BEFORE the correction. That is the correction working, not a
            # discrepancy — but say what it replaced, so it stays checkable.
            verdict = "CORRECTED"
        elif conflict_in:
            verdict = "CONFLICT"
        elif seen_in:
            verdict = "CORROBORATED"
        elif k in salvaged and _same(t, salvaged[k]):
            verdict = "SALVAGED"
        else:
            verdict = "UNCORROBORATED"
        tally[verdict] += 1
        note = ""
        if verdict == "CORROBORATED":
            note = f"in {len(seen_in)} copy(ies)"
        elif verdict == "CONFLICT":
            note = "differs in " + ", ".join(conflict_in)
        elif verdict == "CORRECTED":
            try:
                a = json.loads(attr)
                note = (f"replaced a fabricated close of {a.get('replaced_net'):+.2f} "
                        f"(older copies still show it)")
            except Exception:
                note = "deliberately corrected; older copies still show the old value"
        elif verdict == "SALVAGED":
            note = "recovered from the corrupt file"
        else:
            note = "newer than every snapshot" if float(t["ts_open"]) > max(
                [float(o["ts_open"]) for rows in sources.values() for o in rows] or [0]) else "NOT in any copy"
        lines.append((verdict, f"  {verdict:<14} {t['symbol']:<6} {t['strategy']:<13} "
                               f"{_fmt(t['ts_open'])} -> {_fmt(t['ts_close'])} "
                               f"{float(t['net'] or 0):+8.2f}   {note}"))

    for verdict, line in lines:
        if show_all or verdict in ("CONFLICT", "UNCORROBORATED", "SALVAGED", "CORRECTED"):
            print(line)
    if not show_all:
        print(f"  (+{tally['CORROBORATED']} corroborated rows hidden — pass --all to list them)")

    print(f"\n  corroborated   {tally['CORROBORATED']:>3}   identical in an independently written copy")
    print(f"  salvaged       {tally['SALVAGED']:>3}   recovered from the destroyed file, provenance stamped")
    print(f"  corrected      {tally['CORRECTED']:>3}   deliberately fixed; older copies hold the old value")
    print(f"  uncorroborated {tally['UNCORROBORATED']:>3}   only in the live ledger")
    print(f"  conflict       {tally['CONFLICT']:>3}   same trade, different numbers")

    gaps = DATA / "data-gaps.json"
    if gaps.exists():
        for g in json.loads(gaps.read_text()).get("gaps", []):
            print(f"\n  DECLARED GAP  {g['from_label']} → {g['to_label']}")
            for l in g.get("known_lost", []):
                print(f"     lost: {l[:150]}")
    return 1 if tally["CONFLICT"] else 0


if __name__ == "__main__":
    sys.exit(main("--all" in sys.argv))
