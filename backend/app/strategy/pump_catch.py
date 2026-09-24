"""volume_build's trigger on a rolling 60-minute window, read every 15 minutes.

WHY THIS EXISTS -- PENGU, 2026-09-19
------------------------------------
The run was 11:39 -> 12:19 Austin (0.00798 -> 0.00830, 2.1M coins in the 11:52
minute). volume_build saw it when the FORMING calendar hour's volume crossed
1.5x normal, on hourly bars refreshed every ten minutes, and bought at 12:24:05
for 0.008301 -- the top. Over three days every open it made landed 1 to 57
minutes after its hourly bar. That is not a bug in the rule; it is the
resolution of the clock the rule reads. A forty-minute move is below it.

The operator's request, verbatim: "the same trigger on a rolling 60-min window
of 1-minute bars, replayed over the 4 years of history against the
calendar-hour version. If it fires earlier without more false entries, promote
it."

WHY 15-MINUTE BARS AND NOT 1-MINUTE
-----------------------------------
Two reasons, both measured in this repo rather than chosen:

  * The 14-day volume normal the trigger compares against is 20,160 one-minute
    bars, five times the 4,000-bar ceiling the live loop allows a panel (it was
    a 5,621 x 75 minute panel that got the backend SIGKILLed fifteen times).
    On 15-minute bars the same normal is 1,344 bars and fits.
  * One-minute history is weeks and cannot be backfilled (data availability
    table in CLAUDE.md). Fifteen-minute history is one to two years, so the
    "replay it against the calendar-hour version" half of the request is
    possible -- see research/rolling_entry.py, which is what decides whether
    this rule is promoted.

So: the identical rule (same volume multiple, same 2-hour climb, same 48-hour
trend gate, same not-red-today gate, same exit) with every hour-denominated
parameter expressed in 15-minute bars, and the engine refreshing this panel
every 5 minutes (_REFRESH_TTL_S[900]). Worst case it sees a crossing 5 to 15
minutes after it happens instead of up to 60.

It is a separate strategy rather than a parameter on volume_build because they
will hold positions in the same coin on different clocks and the book keeps one
position per (symbol, strategy); and because the lab has to be able to compare
them as two rows, not one row that changed under it.

Runs in PAPER, expected edge 0, every fill an experiment -- exactly as
volume_build does. Nothing here can promote itself.
"""
from __future__ import annotations

from app.strategy.volume_build import VolumeBuild

BAR_SECONDS = 900
BPH = 3600 // BAR_SECONDS          # bars per hour = 4


class PumpCatch(VolumeBuild):
    name = "pump_catch"
    version = "0.1"
    card = "pump_catch"

    bar_seconds = BAR_SECONDS

    @staticmethod
    def defaults() -> dict:
        # volume_build's defaults, with every HOUR-denominated count in
        # 15-minute bars. Written out in full rather than derived, because
        # test_silent_failures reads this dict statically to check that every
        # grid key is a real parameter -- and tests/test_pump_catch.py checks
        # that these values really are volume_build's, scaled.
        return {
            "window_bars": 1 * BPH,          # the last 60 minutes, rolling
            "baseline_bars": 336 * BPH,      # the same 14-day normal
            "volume_multiple": 1.5,
            "climb_bars": 2 * BPH,           # climbed over the last 2 hours
            "climb_pct": 3.0,
            "trend_bars": 48 * BPH,          # the same two-day picture
            "trend_min_pct": 10.0,
            "trend_oversold_pct": -10.0,
            "day_min_pct": 0.0,
            "profit_margin_pct": 5.0,
            "give_up_hours": 36,
            "catastrophe_stop_pct": 15.0,
            "max_signals_per_bar": 0,
            "max_extended_pct": 25.0,
            "expected_edge_bps": 0.0,
            "calib_n": 0,
            "calib_note": ("uncalibrated by design; volume_build's trigger on a rolling "
                           "60-minute window; promotion decided by research/rolling_entry"),
        }
