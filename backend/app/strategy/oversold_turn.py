"""Buy the turn after capitulation, not the fall into it.

The operator's observation, on a day when 32 of 33 tradable coins were red:
"things got oversold and then started coming back up -- these are good ones to
buy oversold low and hold, then sell when it is climbing up gradually."

This is the opposite side of volume_build. That one buys strength: volume rising
INTO a climb, in an uptrend, green on the day. This one buys a coin that has
been beaten down and has just stopped falling. They do not compete -- they fire
in different weather.

WHY IT WAITS FOR THE TURN
-------------------------
The distinction the operator drew is the whole strategy: a coin still falling is
a knife, a coin that has stopped falling is a bounce. So the rule never fires on
depth alone. It needs the bar before it to have been falling and THIS bar to
have closed up -- the first higher close after a decline. That is observable at
the moment of entry, which "it bounced off the low" is not: on any day still
running, the low sits near the end, so every red day looks like a bounce in
hindsight. Measured today: the median day low sat 94% of the way through the
session, and only 1 of 33 coins made its low in the first half and recovered.

WHAT WAS MEASURED
-----------------
35,139 hourly bars x 33 API-tradable coins, 2022-09 to 2026-09. Non-overlapping,
one entry per coin per day, exits applied as the engine applies them, every
result net of the 1.92% round trip. Grid fitted on the first three years, the
winner read ONCE on the held-out final year.

  depth below the 24h high    train avg net
     12%                        -0.80%   (70.4% profitable)
     15%                        +1.03%   (69.6% profitable)
     20%                        +1.83%   (80.0% profitable)

Deeper is monotonically better -- shallow dips are noise, real capitulation
bounces. The held-out year: 43 trades, +0.85% avg net, 67.4% profitable.

HONESTLY, THE LIMITS
--------------------
t = 0.70 on the held-out year. That is NOT statistical significance, and this
configuration was the best of 69 tried. It is not outlier-driven -- the upside
is capped at the target, so dropping the best five trades still leaves +0.16% --
but it rests on 43 observations, of which 5 hit the stop at about -17%. Three
more stop-outs would erase the edge entirely. 20% was also the deepest drop
tested, so the true optimum may be deeper still, or may not exist.

So this runs in paper, alongside volume_build, to earn forward evidence. It is
the first configuration in this project to clear the round trip out of sample,
and that is a reason to collect more data on it, not a reason to believe it yet.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel, Signal, Strategy

ROUND_TRIP_PCT = 1.9182          # (1+s)/(1-s)-1 at 0.95% per side


class OversoldTurn(Strategy):
    name = "oversold_turn"
    version = "0.1"
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            "lookback_bars": 24,        # the high it has fallen away from
            "drop_pct": 20.0,           # how far below that high before it counts
            # target_pct = ROUND_TRIP + this. 6.08 makes the gross target 8%,
            # which is the number that was actually measured.
            "profit_margin_pct": 6.08,
            "give_up_hours": 48,
            "catastrophe_stop_pct": 15.0,
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
        }

    def warmup_bars(self) -> int:
        return int(self.params["lookback_bars"]) + 5

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately does nothing.

        Leaving the expected edge at zero means every fill is logged as an
        experiment rather than as a claim. This strategy has not earned a claim.
        """
        return None

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars() or t < 2:
            return []
        L = int(p["lookback_bars"])
        drop = float(p["drop_pct"])
        close, high = panel.close, panel.high
        if high is None:
            return []

        window = high[t - L + 1: t + 1]
        with np.errstate(invalid="ignore"):
            peak = np.nanmax(window, axis=0) if window.size else np.full(panel.N, np.nan)

        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            px, prev, prev2 = close[t, k], close[t - 1, k], close[t - 2, k]
            pk = peak[k]
            if not (np.isfinite(px) and np.isfinite(prev) and np.isfinite(prev2)):
                continue
            if not (np.isfinite(pk) and pk > 0):
                continue
            depth = (px / pk - 1.0) * 100.0
            if depth > -drop:
                continue                      # not beaten down enough to be capitulation
            if not (px > prev):
                continue                      # still falling -- this is the knife
            if not (prev <= prev2):
                continue                      # was not falling into it; no turn to buy

            target_pct = ROUND_TRIP_PCT + float(p["profit_margin_pct"])
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy",
                raw_score=float(-depth),      # how deep the hole is
                expected_edge_bps=0.0,
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["give_up_hours"]) * 3600.0,
                stop_bps=float(p["catastrophe_stop_pct"]) * 100.0,
                target_bps=target_pct * 100.0,
                features={"depth_below_high_pct": float(depth),
                          "lookback_hours": float(L),
                          "turned_up_pct": float((px / prev - 1.0) * 100.0),
                          # Depth was the monotonic feature in testing, so it is
                          # what sizing scales on: 1.0x at the entry threshold,
                          # more as the hole gets deeper.
                          "conviction_x": float(-depth / drop) if drop > 0 else 1.0,
                          "keeps_after_costs_pct": float(p["profit_margin_pct"]),
                          "calibration_n": 0},
            ))
        # Deepest hole first: depth is the feature that was monotonic in testing.
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
