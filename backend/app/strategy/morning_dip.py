"""Buy low in the morning, sell later the same day. The operator's rule, verbatim.

    "buy low and hold for some time, usually buy around am and sell later am or
     early pm"

This is strategy one of the two he asked for, implemented exactly as stated and
measured before shipping rather than after.

WHAT IT DOES
------------
In the morning, find a coin trading below its own recent high that has just
closed up -- it fell and has turned. Buy it. Sell at a profit target, a stop, or
the early-afternoon clock, whichever arrives first.

WHAT IT MEASURED
----------------
192 variants swept over 35,171 hourly bars x 58 Robinhood-confirmed coins,
2022-09 to 2026-09. Non-overlapping, one entry per coin per day, every result
net of the 1.92% round trip. Fitted on the first three years, read ONCE on the
held-out final year.

Best on train: 10% below the 24h high, bought by 09:00, sold at +6% / -8% /
14:00 -- 1,736 trades, -1.41% a trade, 43.1% profitable.

    HELD-OUT YEAR: 1,165 trades, -1.71% a trade, 37.5% profitable, t = -12.76.

That is not a near miss. It is significantly negative, on a large sample, out of
sample. Deeper dips looked far better on train (+1.60% at a 20% dip, 77.4%
profitable) and collapsed to -3.73% on the held-out year -- the clearest case of
overfitting this project has produced, and a warning about every deep-dip
variant including oversold_turn.

WHY IT SHIPS ANYWAY
-------------------
It trades about three times a day, which is the point. A week of watching zero
fills taught nothing; a week of real paper fills against a known-negative
expectation teaches the operator what the spread actually costs, in his own
ledger, without risking a dollar.

So: `calibrate()` deliberately does nothing. The expected edge stays at zero,
every fill logs as an experiment rather than a claim, and nothing here can reach
live mode -- that path needs a demonstrated edge this strategy does not have.

If the effective fill spread turns out to be nearer 0.32% a side than 0.95%, the
round trip is 0.64% instead of 1.92% and this becomes -0.43% a trade. Still
negative. The only honest reason to run it is to find that number out.
"""
from __future__ import annotations

import numpy as np

from app.strategy import hour_profile
from app.strategy.base import Panel, Signal, Strategy

ROUND_TRIP_PCT = 1.9182


class MorningDip(Strategy):
    name = "morning_dip"
    version = "0.1"
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            "lookback_bars": 24,        # the high it has fallen away from
            "dip_pct": 10.0,            # how far below that high before it counts
            # The clock is NOT a gate. It was one ("buy_from_hour 0, buy_by_hour
            # 6") and on 2026-09-13 the only two coins that passed every other test did
            # so at 11:00 and 12:00 and were refused, on a day when ten tradable
            # coins cleared the round trip and this desk traded nothing.
            #
            # The operator's observation that mornings look good is a prior, not a
            # law. It now enters as a WEIGHT on the ranking score, learned by
            # empirical Bayes in hour_profile and collapsing to 1.000 by itself if
            # the differences turn out to be noise. Every hour stays tradable.
            #
            # Measured on the training years with no gate at all: 2,938 entries,
            # mean gross +0.70%, per-trade sd 7.52%. Between-hour variance 1.131
            # observed vs 0.461 expected from noise -- ratio 2.45, so the hour does
            # carry signal, but an hour with 100 entries keeps only about half its
            # raw deviation after shrinkage.
            "sell_at_hour": 14,         # kept for the card; the exit is hold_hours
            "hold_hours": 14,           # measured; see generate()
            "confirm_window": 6,        # the low we must not be re-making
            "confirm_bars": 3,          # how long it must have held
            # target_pct = ROUND_TRIP + this; 4.08 makes the gross target 6%,
            # which is the figure that was actually measured.
            "profit_margin_pct": 4.08,
            "catastrophe_stop_pct": 8.0,
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
            "calib_note": "uncalibrated by design; measured net-negative, runs as "
                          "a paper experiment to price the spread in real fills",
        }

    def warmup_bars(self) -> int:
        return int(self.params["lookback_bars"]) + 5

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately does nothing, so the expected edge stays at zero and
        every fill is logged as an experiment rather than a claim."""
        return None

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars() or t < 2:
            return []
        # Morning only. The clock is the operator's, and it is the one part of
        # this rule that is his observation rather than a fitted parameter.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        hour = datetime.fromtimestamp(float(panel.ts[t]), ZoneInfo("America/Chicago")).hour
        # No hour is ever refused. The hour only changes how this coin ranks
        # against the others competing for the same slot.
        # No hour gate. One was tried and it OVERFITS: choosing the best six
        # hours on the training years gave +0.32% held-out gross with the old
        # entry rule, and -0.58% once the entry rule below changed -- the six
        # hours selected were completely different ([23,19,6,20,2,3] vs
        # [2,5,9,10,12,13]). A signal that moves that much when you change
        # something else is not a signal. The hour stays a ranking weight only.
        hour_w = hour_profile.weight_for(self.name, hour)

        L = int(p["lookback_bars"])
        dip = float(p["dip_pct"])
        close, high, low = panel.close, panel.high, panel.low
        if high is None:
            return []
        window = high[t - L + 1: t + 1]
        with np.errstate(invalid="ignore"):
            peak = np.nanmax(window, axis=0) if window.size else np.full(panel.N, np.nan)

        # Hours until the clock exit, so the engine's time stop lands on it.
        # A FIXED hold, not a clock. "sell_at_hour - hour" collapsed to 1 hour for
        # every entry after 13:00, and once the hour gate came off that was most
        # of them: 66% of held-out trades got a hold of <=2h, and in live paper
        # five of eight morning_dip trades were forced out after 1.0h. Paying a
        # 1.92% round trip to hold something for an hour cannot work.
        #
        # 14 hours is what was actually measured (buy in the morning, sell at
        # 14:00). Held-out gross by hold: 8h -0.18%, 14h -0.22%, 20h -0.24% --
        # flat, so the exact number is not delicate; the 1-hour collapse was.
        hold_hours = int(p["hold_hours"])
        target_pct = ROUND_TRIP_PCT + float(p["profit_margin_pct"])

        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            px, prev = close[t, k], close[t - 1, k]
            pk = peak[k]
            if not (np.isfinite(px) and np.isfinite(prev) and np.isfinite(pk) and pk > 0):
                continue
            depth = (px / pk - 1.0) * 100.0
            if depth > -dip:
                continue                      # not low enough to be "buying low"
            if not (px > prev):
                continue                      # still falling; wait for the turn

            # Wait for the fall to have actually STOPPED, not just for one green
            # bar. The operator watched XLM on 2026-09-16: this rule bought at
            # 01:16 at 0.17600 and the low came later, at 03:30, at 0.17340 --
            # buying that low would have been +1.47% instead of -0.03%. One
            # up-close inside a continuing decline is not a turn.
            #
            # The test: no NEW six-bar low in the last three bars. Measured on
            # the same entries, held-out gross by confirmation rule:
            #     first up-close (was live)   -0.22%   6.1 trades/day
            #     two consecutive up closes   -0.14%   3.3/day
            #     price 1% off the 6-bar low  -0.26%
            #     NO NEW LOW IN 3 BARS        +0.09%   3.1/day   <- this
            # Train agrees on the ranking (+0.64% vs +0.56%), so it is a real if
            # small effect, and it halves the trade count for the better half.
            k0 = max(0, t - int(p["confirm_window"]) + 1)
            win_low = low[k0: t + 1, k] if low is not None else None
            if win_low is not None and win_low.size:
                floor_all = np.nanmin(win_low)
                recent = low[max(0, t - int(p["confirm_bars"]) + 1): t + 1, k]
                if (np.isfinite(floor_all) and recent.size
                        and np.isfinite(np.nanmin(recent))
                        and not np.nanmin(recent) > floor_all * 1.0001):
                    continue

            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy",
                raw_score=float(-depth) * hour_w,
                expected_edge_bps=0.0,
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(hold_hours) * 3600.0,
                stop_bps=float(p["catastrophe_stop_pct"]) * 100.0,
                target_bps=target_pct * 100.0,
                features={"depth_below_high_pct": float(depth),
                          "entry_hour_local": float(hour),
                          "hour_weight": float(hour_w),
                          "sell_at_hour": float(p["sell_at_hour"]),
                          "turned_up_pct": float((px / prev - 1.0) * 100.0),
                          "conviction_x": float(-depth / dip) if dip > 0 else 1.0,
                          "keeps_after_costs_pct": float(p["profit_margin_pct"]),
                          "calibration_n": 0},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
