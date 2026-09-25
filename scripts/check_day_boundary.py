#!/usr/bin/env python3
"""Fail if SQL decides a trading day with `localtime`.

WHY -- 2026-09-22
-----------------
SQLite's `date(ts,'unixepoch','localtime')` uses the timezone of whatever
machine runs the query. On the operator's Mac that is America/Chicago and the
answer is right. Anywhere else -- CI, the assistant's Linux sandbox, a container
-- it is UTC, and UTC midnight is 19:00 Chicago. So the last five hours of every
evening quietly land on the next day.

The app itself was never wrong: `core/clock.py` exists precisely to give one
definition of "today", and daily_report and guards both go through it. The ad-hoc
SQL beside it did not, and every day-by-day table the assistant produced from the
Linux side was shifted five hours before anyone noticed.

A fixed offset ('-5 hours') is not a fix: Chicago is UTC-6 for four months of the
year. Bucket in Python through `clock.day_key`, or filter on unix bounds from
`clock.bounds_for_day`.

An intentional use -- a coverage count where a five-hour shift cannot change a
decision, and where pulling millions of rows into Python to be exact would cost
more than it is worth -- declares itself on the same line:

    # day-boundary-ok: <why it does not matter here>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "backend" / "app"

# 'localtime' inside a SQL string. `time.localtime(...)` is Python, a different
# thing, and is not what this rule is about.
SQL_LOCALTIME = re.compile(r"""['"]\s*localtime\s*['"]""")
WAIVER = re.compile(r"#\s*day-boundary-ok:\s*\S")


def main() -> int:
    bad, waived = [], []
    # clock.py is the module that DEFINES the right way; its docstring quotes the
    # wrong pattern in order to warn about it, which is not a use of it.
    for f in sorted(APP.rglob("*.py")):
        if f.name == "clock.py" and f.parent.name == "core":
            continue
        try:
            lines = f.read_text().splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            if not SQL_LOCALTIME.search(line):
                continue
            rel = f.relative_to(ROOT)
            # The waiver may sit on this line or in the comment block just above
            # it -- a real reason often needs more than one line to state.
            ctx = " ".join(lines[max(0, i - 5):i])
            (waived if WAIVER.search(ctx) else bad).append(f"{rel}:{i}  {line.strip()[:90]}")
    for w in waived:
        print(f"  waived  {w}")
    if bad:
        print("\ncheck_day_boundary: SQL is deciding a day with 'localtime' —")
        print("that is the timezone of whatever machine runs it, and UTC midnight")
        print("is 19:00 Chicago. Use clock.day_key / clock.bounds_for_day, or add")
        print("'# day-boundary-ok: <reason>' if a five-hour shift genuinely cannot")
        print("change the answer.\n")
        for b in bad:
            print(f"   {b}")
        return 1
    print(f"check_day_boundary: OK — no SQL defines a day by machine timezone "
          f"({len(waived)} declared exception(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
