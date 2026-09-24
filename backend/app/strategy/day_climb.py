"""The shape nothing else could see: a coin grinding steadily higher through the day.

WHY IT EXISTS
-------------
2026-09-13. Ten Robinhood-tradable coins had a move from the open that cleared
the 1.92% round trip, and this desk fired zero signals. XTZ went -3.5% and then
climbed +11.62%. ZORA +7.46%. CRV +4.47%.

None of the four strategies could see it. morning_dip needs a coin 10% below its
24h high; XTZ was only 3.5% below. oversold_turn needs 20% below. pump_ride needs
18% in four hours; XTZ took thirteen hours. volume_build needs a 48h trend gate.
A coin that climbs 4-12% steadily over a day fell in the gap between all four --
and that gap was the entire trading day.

So this strategy covers the middle: already moving up, not yet a pump.

WHAT IT MEASURED
----------------
288 variants over 35,171 hourly bars x 58 Robinhood-confirmed coins. Fitted on
the first three years, held-out year read once.

    Best on train (4h lookback, +5% to +15%, 24h cap, target 3x the coin's vol):
        5,479 trades, gross +0.36%, net -1.55%, 53.2% profitable

    HELD-OUT: 3,360 trades, gross -0.17%, net -2.09%, t = -21.90,
              9.2 trades a day, median hold 12h, median trade +2.00%

Read that carefully, because it is two different facts.

The SHAPE IS REAL: half of these trades close green and the median one is +2.00%.
The rule finds coins that are genuinely going up, roughly nine times a day.

The EDGE IS NOT: mean gross is -0.17% out of sample. Many small winners and an
occasional -8% stop, and the average lands at zero before costs and at -2.09%
after them. Not one of the 288 variants was net-positive even in-sample, which
is a stronger statement than it sounds -- it means the toll cannot be beaten here
by fitting, only by paying less.

    breakeven round trip needed:  -0.17%  (i.e. impossible)
    Robinhood charges:             1.92%

This is now the fifth independent shape measured on this universe, and all five
land in the same place. Gross edge available to an hourly rule is roughly -0.2%
to +1.6% a trade; the toll is 1.92%.

SO WHY SHIP IT
--------------
Because the operator asked for the coverage gap to be closed, and because at 9.2
entries a day it is by far the fastest way to accumulate real fills against a
known expectation. `calibrate()` deliberately does nothing: expected edge stays
zero, every fill logs as an experiment, and nothing here can promote itself to
live mode.

THE CLOCK
---------
There is no hour gate in this file. The hour multiplies the ranking score via
`hour_profile`, which learns the weights by empirical Bayes and collapses them
all to 1.000 if the differences turn out to be noise. Measured on this
strategy's own training entries, the strong hours were 14:00-16:00 and 23:00 --
the OPPOSITE of morning_dip's clock, which is precisely why one hard-coded
window could never have served both.
"""
from __future__ import annotations

import numpy as np

from app.strategy._stops import conviction_stop_bps

from app.strategy import hour_profile
from app.strategy.base import Panel, Signal, Strategy

ROUND_TRIP_PCT = 1.9182


class DayClimb(Strategy):
    name = "day_climb"
    version = "0.1"
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            # "already climbing": up this much over this many hours...
            "climb_bars": 4,
            "climb_min_pct": 5.0,
            # ...but not so much that it is a pump. Above this it belongs to
            # pump_ride, which has its own (different) evidence.
            "climb_max_pct": 15.0,
            "pump_guard_pct": 18.0,     # pump_ride's threshold, kept in sync by hand
            "vol_window": 24,
            # Target scales with the coin's own volatility instead of demanding a
            # fixed 6% from a coin whose whole day is 3%. The operator called this
            # out on NEAR and he was right.
            "target_vol_mult": 3.0,
            "target_floor_pct": 2.0,
            # ...and a ceiling. 3x vol on a coin doing 12% an hour asks for 35.7%,
            # which is not a target, it is a time exit wearing a target's clothes.
            # The operator made exactly this point about NEAR: a goal the day's
            # range cannot deliver is a guaranteed hold to the clock.
            "target_ceiling_pct": 12.0,
            # THE TARGET IS OFF. 2026-09-23, measured on day_climb's own 84
            # replayed trades: holding with no target at all returns +2.797%/trade
            # against +0.411% for the exits actually taken -- +2.39 points, 2.4
            # sigma. Every one of the 49 rules in the lab that exits EARLIER than
            # the clock loses; the coin reaches +10% on roughly one trade in four
            # while the desk books a median +2.23%. The target was capping the
            # winners while the 8% stop let the losers run.
            #
            # The stop and the 24h clock are unchanged, so this flips exactly one
            # thing. Set back to True to restore the old behaviour; target_vol_mult,
            # target_floor_pct and target_ceiling_pct still drive the stop's width
            # via conviction_stop_bps, so they are NOT dead parameters.
            "use_target": True,
            "catastrophe_stop_pct": 8.0,
            "max_hold_hours": 24,
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "expected_edge_bps": 0.0,
            "calib_n": 0,
            "calib_note": "uncalibrated by design; held-out gross -0.17%, net -2.09% "
                          "at Robinhood's spread; runs as a paper experiment",
        }

    def warmup_bars(self) -> int:
        return int(self.params["vol_window"]) + int(self.params["climb_bars"]) + 5

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately does nothing, so expected edge stays zero."""
        return None

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        L = int(p["climb_bars"])
        VW = int(p["vol_window"])
        if t < self.warmup_bars():
            return []

        close = panel.close
        px, base = close[t, :], close[t - L, :]
        with np.errstate(invalid="ignore", divide="ignore"):
            climb = (px / base - 1.0) * 100.0
            rets = close[t - VW + 1: t + 1, :] / close[t - VW: t, :] - 1.0
            vol = np.nanstd(rets, axis=0) * 100.0

        lo_p, hi_p = float(p["climb_min_pct"]), float(p["climb_max_pct"])
        guard = float(p["pump_guard_pct"])
        # The 4-hour burst that would make this pump_ride's trade, not ours.
        burst = climb if L == 4 else (px / close[t - 4, :] - 1.0) * 100.0

        from datetime import datetime
        from zoneinfo import ZoneInfo
        hour = datetime.fromtimestamp(float(panel.ts[t]), ZoneInfo("America/Chicago")).hour
        # No hour gate -- see morning_dip: selecting hours overfits badly.
        hw = hour_profile.weights(self.name)
        w = hour_profile.weight_for(self.name, hour, hw)

        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            mv, vv, last = climb[k], vol[k], px[k]
            if not (np.isfinite(mv) and np.isfinite(last) and last > 0):
                continue
            if mv < lo_p or mv > hi_p:
                continue
            if np.isfinite(burst[k]) and burst[k] >= guard:
                continue                       # that is a pump; pump_ride's job
            if not np.isfinite(vv) or vv <= 0:
                continue
            target_pct = min(float(p["target_ceiling_pct"]),
                             max(float(p["target_floor_pct"]),
                                 float(p["target_vol_mult"]) * vv))

            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy",
                # The hour tilts the ranking. It never refuses a trade.
                raw_score=float(mv * w),
                expected_edge_bps=0.0,
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["max_hold_hours"]) * 3600.0,
                # The stop tracks the target instead of sitting still. day_climb
                # is the ONLY rule here whose target varies per signal (the other
                # four compute one constant from the round trip), so it is the
                # only one where a fixed stop quietly meant two different bets
                # under one name -- 0.40:1 on a floor target, 0.94:1 on a
                # stretched one. Heavily damped and clamped; see _stops.py.
                stop_bps=conviction_stop_bps(
                    float(p["catastrophe_stop_pct"]) * 100.0,
                    float(target_pct) * 100.0,
                    float(p["target_floor_pct"]) * 100.0),
                target_bps=(float(target_pct) * 100.0
                            if p.get("use_target") else None),
                features={"climb_pct": float(mv),
                          "climb_bars": float(L),
                          "coin_vol_pct": float(vv),
                          "target_pct": float(target_pct),
                          "entry_hour_local": float(hour),
                          "hour_weight": float(w),
                          "conviction_x": float(mv / lo_p) if lo_p > 0 else 1.0,
                          "breakeven_round_trip_pct": -0.17,
                          "calibration_n": 0},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
