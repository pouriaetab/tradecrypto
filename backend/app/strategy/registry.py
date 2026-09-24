from __future__ import annotations

from app.strategy.base import Strategy
from app.strategy.fast_flip import FastFlip
from app.strategy.forced_momentum import ForcedMomentum
from app.strategy.lead_lag_rotation import LeadLagRotation
from app.strategy.morning_mover import MorningMover
from app.strategy.day_climb import DayClimb
from app.strategy.morning_dip import MorningDip
from app.strategy.pump_ride import PumpRide
from app.strategy.pump_catch import PumpCatch
from app.strategy.burst_catch import BurstCatch
from app.strategy.oversold_turn import OversoldTurn
from app.strategy.volume_build import VolumeBuild
from app.strategy.regime_swing import RegimeSwing
from app.strategy.top_mover_reversal import TopMoverReversal
from app.strategy.xs_momentum import XSMomentum

STRATEGIES: dict[str, type[Strategy]] = {
    FastFlip.name: FastFlip,
    ForcedMomentum.name: ForcedMomentum,
    RegimeSwing.name: RegimeSwing,
    LeadLagRotation.name: LeadLagRotation,
    TopMoverReversal.name: TopMoverReversal,
    XSMomentum.name: XSMomentum,
    MorningMover.name: MorningMover,
    VolumeBuild.name: VolumeBuild,
    OversoldTurn.name: OversoldTurn,
    DayClimb.name: DayClimb,
    MorningDip.name: MorningDip,
    PumpRide.name: PumpRide,
    PumpCatch.name: PumpCatch,
    BurstCatch.name: BurstCatch,
}

# The four the operator originally described.
OPERATOR_STRATEGIES = ["fast_flip", "forced_momentum", "regime_swing", "lead_lag_rotation"]

# What actually runs. Measured, not assumed:
#   xs_momentum   +60.7 bps out of sample at a 1-day rank. The only positive.
#   fast_flip      +3.9 bps. Kept as the control -- a strategy with no edge is
#                  the reference every other number is judged against.
#   regime_swing    0 bps, confidence interval straddles zero.
#   lead_lag        BTC's move predicts an alt's next move with a correlation of
#                  -0.017. Retired; leaving it in the roster implied it did
#                  something.
#   morning_mover  The operator's own idea, finally built. Buys a coin whose
#                  volume is already >=1.5x its normal for that slice of the day,
#                  between 01:00 and 09:00 Austin, and holds it through the
#                  afternoon. The entry window and the volume threshold are both
#                  measured, not guessed (see the module docstring). It has NOT
#                  been shown to beat its cost -- holding to the close from a
#                  hot-volume morning entry averaged +1.28% gross against a 1.92%
#                  round trip. It runs in paper to build a real sample, because
#                  the alternative is another month of arguing about backtests.
# Two, deliberately opposite. volume_build buys strength -- volume rising into a
# climb, in an uptrend, green on the day. oversold_turn buys the first higher
# close after a coin has fallen 20% from its 24h high. They fire in different
# weather, which is the point: on a day like 2026-09-10, when 32 of 33 tradable
# coins were red and not one passed volume_build's climb gate, the only setup
# available was the bounce.
# Four, covering both shapes the operator described plus the capitulation bounce.
# morning_dip is the one that produces volume -- about three entries a day -- and
# it is measured NET NEGATIVE out of sample (-1.71% a trade, t = -12.76). It runs
# in paper to price the spread in real fills, not because the backtest endorses
# it. pump_ride is the opposite: 0.29 entries a day, held-out t = -0.32, which is
# undecided rather than bad, and the only one of the four whose gross edge
# (+1.56%) is within sight of Robinhood's 1.92% round trip.
#
# What all four share is the same verdict from four independent directions: the
# gross edge available to an hourly rule on this universe is 0.2% to 2.8% a
# trade, and the toll is 1.92%. Breakeven round trip, held out:
#
#     morning_dip    0.21%      needs 0.10% a side
#     volume_build   0.98%      needs 0.49% a side
#     pump_ride      1.56%      needs 0.77% a side
#     oversold_turn  2.77%      needs 1.37% a side  (n=43, t=0.70 -- not real)
#
# Robinhood's spread, read off their own quotes for 33 coins: 0.855% to 0.981% a
# side. The cheapest coin on the platform is dearer than three of the four rules
# can survive. Nothing here can reach live mode: all four leave expected edge at
# zero by design.
# Five, and the fifth exists because of a specific day. On 2026-09-13 ten
# tradable coins cleared the round trip and this desk fired ZERO signals: XTZ
# climbed +11.62% while sitting only 3.5% below its 24h high, which is invisible
# to a dip rule, too slow for a pump rule, and outside volume_build's trend gate.
# day_climb covers that middle. It trades 9.2 times a day and its held-out gross
# is -0.17%, so it closes a coverage hole rather than adding an edge.
#
# The scoreboard, all held out, all net of 1.9182%:
#     morning_dip    gross +0.21%   breakeven needs 0.10%/side
#     day_climb      gross -0.17%   no cost clears it
#     volume_build   gross +0.98%   needs 0.49%/side
#     pump_ride      gross +1.56%   needs 0.77%/side
#     oversold_turn  gross +2.77%   needs 1.37%/side (n=43, t=0.70 -- not real)
# Robinhood's measured spread: 0.855%-0.981% a side. Five shapes, one answer.
# Six: pump_catch is volume_build's trigger on a rolling 60-minute window of
# 15-minute bars, added 2026-09-19 after PENGU was bought at the top of a
# forty-minute move that an hourly clock could not see. It runs beside
# volume_build, not instead of it, so the lab can compare the two clocks on the
# same coins -- research/rolling_entry.py is the judge.
# Seven: burst_catch (2026-09-21) is the first-hour-of-a-burst rule -- +3% from
# three hours ago, inside the last hour, on volume, small target, hard 90-minute
# limit -- built from the twelve coins the desk bought at the top on 09-20.
# burst_catch came off this list on 2026-09-21, one day after it went on. It is
# the reason the rule below it now exists: it was written from one afternoon's
# observation and put straight into the paper book, where it took 17 trades in
# about six hours, finished flat on price (gross -$3.95) and paid $17.68 of
# spread for a net -$21.63 -- against roughly $56 the other six had made in nine
# days. Its trades are not deleted; they are in mode='lab', where exit_lab still
# reads them, so it can be tuned against its own real fills and earn its way
# back. Nothing returns to this list on an argument; it returns on a measured
# edge that clears the round trip.
ACTIVE_STRATEGIES = ["volume_build", "oversold_turn", "morning_dip", "pump_ride",
                     "day_climb", "pump_catch"]
RETIRED = {
    "burst_catch": ("2026-09-21: 17 trades in six hours, gross -$3.95, spread -$17.68, net -$21.63. Its shape needed a 70% win rate to break even at Robinhood's 1.90% round trip (+4% target, -3% stop) and it got 18%. Now cost-gated by shape_survives_costs() and parked in mode='lab' for tuning."),
    "lead_lag_rotation": "BTC->alt correlation is -0.017 at every lag tested",
    "forced_momentum": "0.3 bps average expected edge over 34 signals",
    "top_mover_reversal": "never validated out of sample",
    "xs_momentum": ("it rebalances on a schedule with no notion of whether a coin is "
                    "still building or already finished. On 2026-09-09 it bought NEAR and "
                    "DOT at 09:10 -- NEAR had run from 01:00 to 07:00 and was flat, DOT had "
                    "already made its move. Buying the leaderboard is buying the top. The "
                    "cross-sectional edge it was kept for is real but it is a multi-day "
                    "signal, and this is an intraday book."),
    "morning_mover": ("right idea, wrong constraint: it only looked between 01:00 and "
                      "09:00 and would have missed DOT on 2026-09-08, whose volume went "
                      "to 6.8x at 09:30 and ran +12% by 10:30. Replaced by volume_build, "
                      "which has no entry hour and no exit clock."),
    "fast_flip": ("its four real paper fills were held 18 seconds to 15 minutes and "
                  "every one lost. A round trip costs 1.92%; a 20-minute hold cannot "
                  "produce that. It served its purpose as the control and is done."),
}


# ── who TRADES vs who is STUDIED ─────────────────────────────────────────────
#
# 2026-09-22. burst_catch was retired: taken off ACTIVE_STRATEGIES, its trades
# moved to mode='lab' so the book stops counting them and exit_lab keeps
# reading them. The operator's understanding -- and the plan -- was that it
# would carry on being studied every day so it could be tuned and earn its way
# back.
#
# It was not. Coming off ACTIVE_STRATEGIES silently stopped SIX daily research
# paths from looking at it: the entry-quality fit, the retrain check, the
# per-day strategy replay, the regime scorecard, the version ledger and the
# relearn cards. Only exit_lab still saw it, because exit_lab reads the trades
# table without consulting any roster. So a strategy parked "for training" was
# in fact frozen, and nothing said so.
#
# The two questions are different and now have different answers:
#
#     ACTIVE_STRATEGIES   may open a position. The execution path only.
#     studied()           has something to learn from. Every research path.
#
# A retired strategy stays in studied() for as long as it still has trades in
# the database. That is what "kept in the lab" has to mean, or it is just a
# nicer word for deleted.
def studied(mode: str = "lab") -> list[str]:
    """Strategies the research jobs should still look at, active or retired.

    Never raises and never returns less than the active roster: a research job
    that cannot reach the database should study the live strategies, not none.
    """
    out = list(ACTIVE_STRATEGIES)
    seen = set(out)
    try:
        from app.core import db
        for r in db.query("SELECT DISTINCT strategy FROM trades WHERE mode=?", (mode,)):
            n = r["strategy"]
            if n and n not in seen and n in STRATEGIES:
                seen.add(n)
                out.append(n)
    except Exception:
        pass
    return out


# Parameter grids used by the Model Lab's walk-forward. Kept here so the number
# of configurations searched is explicit and countable -- the deflated Sharpe
# depends on being honest about it.
PARAM_GRIDS: dict[str, dict[str, list]] = {
    "volume_build": {"window_bars": [1, 4], "volume_multiple": [2.0, 3.0, 5.0],
                     "climb_bars": [1, 2, 3], "climb_pct": [2.0, 3.0],
                     "trend_bars": [24, 48], "trend_min_pct": [2.0, 10.0, 20.0],
                     "trend_oversold_pct": [-20.0, -10.0, -5.0],
                     # Must span the live default (5.0), or walk-forward is
                     # testing configurations the desk does not run.
                     "profit_margin_pct": [3.0, 5.0, 8.0]},
    # pump_pct must span the live default (18.0) and reach past it, because the
    # held-out gross edge was still RISING at 18% -- the grid edge was binding.
    "day_climb": {"climb_bars": [4, 6, 8], "climb_min_pct": [3.0, 5.0],
                  "climb_max_pct": [10.0, 15.0], "target_vol_mult": [2.0, 3.0],
                  "max_hold_hours": [12, 24]},
    "pump_ride": {"pump_bars": [2, 4, 6], "pump_pct": [12.0, 18.0, 25.0],
                  "trail_pct": [5.0, 8.0, 12.0], "max_hold_hours": [6, 24]},
    "burst_catch": {"burst_pct": [2.0, 3.0, 4.5], "within_bars": [4, 8], "vol_mult": [1.0, 1.5, 2.5],
                    "target_pct": [3.0, 4.0, 6.0], "stop_pct": [2.0, 3.0], "hold_minutes": [60, 90, 150]},
    # Same grid as volume_build, in 15-minute bars (x4 on the hour-counts).
    "pump_catch": {"window_bars": [4, 8], "volume_multiple": [1.5, 2.0, 3.0],
                   "climb_bars": [4, 8, 12], "climb_pct": [2.0, 3.0],
                   "trend_min_pct": [2.0, 10.0, 20.0],
                   "profit_margin_pct": [3.0, 5.0, 8.0]},
    "morning_dip": {"lookback_bars": [24, 48], "dip_pct": [6.0, 10.0, 15.0],
                    "profit_margin_pct": [2.1, 4.08, 6.1],
                    "sell_at_hour": [11, 14, 17]},
    "oversold_turn": {"lookback_bars": [12, 24, 48], "drop_pct": [12.0, 15.0, 20.0],
                      "profit_margin_pct": [1.1, 3.1, 6.08],
                      "give_up_hours": [24, 48]},
    "fast_flip": {"lookback_bars": [5, 10, 20], "hold_bars": [10, 20, 30],
                  "entry_move_bps": [30.0, 50.0, 80.0]},
    "forced_momentum": {"er_window": [20, 30, 45], "er_min": [0.25, 0.35, 0.45],
                        "hold_bars": [30, 45, 90]},
    "regime_swing": {"breadth_min": [0.55, 0.60, 0.70], "entry_lookback": [15, 30, 60],
                     "hold_bars": [120, 240, 480]},
    "lead_lag_rotation": {"leader_window": [30, 60, 120], "leader_move_min_bps": [50.0, 80.0, 150.0],
                          "hold_bars": [30, 60, 120]},
    "top_mover_reversal": {"z_enter": [1.5, 2.0, 2.5], "hold_bars": [15, 30, 60],
                           "lookback_bars": [30, 60]},
    "xs_momentum": {"lookback_bars": [48, 96, 192], "hold_bars": [2, 12, 20, 48], "top_k": [2, 3]},

}



# A SECOND, NARROWER set of grids, used only by the automatic retrainer.
#
# PARAM_GRIDS above is the Model Lab's walk-forward grid, and it is wide on
# purpose -- that job's whole point is to search and then deflate the Sharpe by
# how many configurations it searched. The retrainer is different: it runs
# unattended every twelve hours and promotes the winner, so a wide grid over four
# years of bars would hand it a spectacular parameter set every single time, and
# it would always be the one fitted to that panel's particular accidents.
#
# Each axis here is one question worth asking about a shipped value ("is the hold
# too short?", "is the stop too tight?"), bracketing the live default rather than
# hunting for a peak. Three values, three axes, 27 combinations at most -- small
# enough that beating the incumbent by two standard errors means something.
RETRAIN_GRIDS: dict[str, dict[str, list]] = {
    "day_climb": {"climb_min_pct": [4.0, 5.0, 6.5], "target_vol_mult": [2.5, 3.0, 3.5],
                  "catastrophe_stop_pct": [6.0, 8.0, 10.0]},
    "morning_dip": {"hold_hours": [10, 14, 18], "confirm_bars": [2, 3, 4],
                    "confirm_window": [4, 6, 9]},
    "pump_ride": {"pump_pct": [15.0, 18.0, 22.0], "trail_pct": [6.0, 8.0, 11.0],
                  "pump_bars": [3, 4, 6]},
    "volume_build": {"volume_multiple": [1.5, 2.0, 3.0], "climb_pct": [2.0, 3.0, 4.5],
                     "profit_margin_pct": [3.0, 5.0, 8.0]},
    "pump_catch": {"volume_multiple": [1.5, 2.0, 3.0], "climb_pct": [2.0, 3.0, 4.5],
                   "profit_margin_pct": [3.0, 5.0, 8.0]},
    "burst_catch": {"burst_pct": [2.0, 3.0, 4.5], "target_pct": [3.0, 4.0, 6.0],
                    "hold_minutes": [60, 90, 150]},
    "oversold_turn": {"drop_pct": [15.0, 20.0, 25.0], "give_up_hours": [12, 24, 48],
                      "profit_margin_pct": [1.1, 3.1, 6.08]},
}


def retrain_grid(name: str) -> dict[str, list]:
    return RETRAIN_GRIDS.get(name, {})

def build(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError(f"unknown strategy {name!r}; have {list(STRATEGIES)}")
    return STRATEGIES[name](**params)


def grid(name: str) -> dict[str, list]:
    return PARAM_GRIDS.get(name, {})
