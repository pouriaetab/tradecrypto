"""One definition of "today", for the whole app.

There were three. `risk/guards.py` cut the day at UTC midnight (19:00 Chicago),
`execution/budget.py` and `research/ledger.py` used a hardcoded -6.0 offset, and
`research/dayscan.py` used a real timezone. So the daily loss cap and the trade
counter reset at 19:00 local while the ledger they were meant to agree with cut
at midnight -- a bot that had spent its whole daily loss allowance by 18:00 got a
second one an hour later, and the kill switch never saw it.

The hardcoded -6.0 was also simply wrong for eight months of the year: Chicago is
UTC-5 under daylight time. A real timezone handles that.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")


def now_local() -> datetime:
    return datetime.now(TZ)


def local_day_bounds(when: float | None = None) -> tuple[float, float]:
    """Midnight to midnight in the operator's local day, as unix seconds."""
    ref = datetime.fromtimestamp(when, TZ) if when is not None else now_local()
    start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def local_day_start(when: float | None = None) -> float:
    return local_day_bounds(when)[0]


def day_key(ts: float) -> str:
    """The operator's day a timestamp falls in, as 'YYYY-MM-DD'.

    Use this instead of SQLite's `date(ts,'unixepoch','localtime')` whenever the
    answer is a TRADING DAY. `localtime` is the timezone of whatever machine
    happens to run the query, so the same SQL returns Chicago days on the
    operator's Mac and UTC days anywhere else -- and UTC midnight is 19:00
    Chicago, so the last five hours of every evening land on the next day.

    2026-09-22: every day-by-day table handed to the operator from the assistant's
    Linux side was silently shifted by those five hours. The app itself was right
    (daily_report and guards both go through this module); the ad-hoc SQL beside
    it was not.

    A fixed SQL offset like '-5 hours' is not a fix either -- Chicago is UTC-6 for
    four months of the year, which is the bug this module's docstring already
    describes. Bucket in Python, where the zone knows about daylight time.
    """
    return datetime.fromtimestamp(float(ts), TZ).strftime("%Y-%m-%d")


def bounds_for_day(day: str) -> tuple[float, float]:
    """Unix bounds of one 'YYYY-MM-DD' in the operator's day. SQL filters on
    these rather than on a formatted date, so the comparison is machine-independent."""
    y, m, d = (int(x) for x in day.split("-"))
    start = datetime(y, m, d, tzinfo=TZ)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()
