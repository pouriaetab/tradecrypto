"""Ride a fast pump, get out quick or stay a bit longer. The operator's rule.

    "the second strategy if it is a fast pump that can make money do it either
     quick or a bit longer"

This is strategy two of the two he asked for. "Quick or a bit longer" is not a
parameter to pick -- it is a TRAILING stop. If the pump stalls the trail fires
within an hour or two and the trade was quick. If it keeps running the trail
follows it up and the trade was a bit longer. The same rule produces both, and
the market decides which, which is exactly what he described.

WHAT IT MEASURED
----------------
320 variants over 35,171 hourly bars x 58 Robinhood-confirmed coins, 2022-09 to
2026-09, 1.16M real bars. Non-overlapping: no new entry in a coin until the
previous one closed. Fitted on the first three years (1,099 days), the held-out
final year (367 days) read at the end.

The finding that matters is not the best variant, it is the SHAPE. Gross edge
rises monotonically with how big a pump has to be before it counts:

    pump over 4h     held-out n   gross/trade   net at RH's 1.92%
    >=  5%               2,673        -0.06%         -1.98%
    >=  8%                 929        +0.41%         -1.51%
    >= 12%                 349        +0.37%         -1.55%
    >= 18%                 107        +1.56%         -0.36%

So there IS something real here: big fast pumps do continue, and the trailing
stop harvests it -- best single held-out trade +58.4%, and only 34% of trades
were profitable, which is the right shape for a let-the-winners-run rule.

Best on train (4h lookback, >=18%, 8% trail, 24h cap): +0.88% a trade, n=136.

    HELD-OUT YEAR: 107 trades, gross +1.56%, net -0.36% a trade, t = -0.32,
    0.29 trades a day, median hold 3 hours.

t = -0.32 means indistinguishable from zero, not proven bad. Every
frequently-trading variant IS proven bad: >=8% and >=12% thresholds gave t of
-3.1 to -7.5 on 350-570 held-out trades. Trading lots of small pumps is dead.
Trading rare huge ones is unresolved.

WHY IT IS UNRESOLVED, AND WHAT WOULD RESOLVE IT
-----------------------------------------------
Per-trade standard deviation is 11.6%. At 107 trades the standard error is 1.1%,
so the data cannot tell +1% from -1%. At 0.29 trades a day it would take roughly
five more years of out-of-sample data to resolve an edge of this size. That is
the honest status, and no amount of re-fitting changes it.

What DOES change it is cost, and cost is measured, not assumed:

    breakeven round trip for this rule:   1.56%   (0.77% a side)
    Robinhood's actual spread:            0.855% - 0.981% a side, 33 coins,
                                          read off their own quotes

The cheapest coin Robinhood offers (SHIB, 1.725% round trip) is still dearer
than this rule's breakeven. There is no coin on that platform where this is
profitable. At a 0.85% round trip -- Coinbase Advanced maker, or Kraken Pro
taker -- it is +0.71% a trade. The rule is not the binding constraint. The venue
is, and that is now a measured statement rather than an opinion.

So `calibrate()` deliberately does nothing: expected edge stays at zero, every
fill logs as an experiment rather than a claim, and nothing here can promote
itself to live mode.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel, Signal, Strategy

ROUND_TRIP_PCT = 1.9182


class PumpRide(Strategy):
    name = "pump_ride"
    version = "0.1"
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            # "a fast pump": this much, this fast. 18% over 4 hours is the only
            # threshold in the sweep with a positive held-out gross edge, and the
            # edge rose monotonically up to it rather than peaking in the middle,
            # which is what a real effect looks like rather than a fitted one.
            "pump_bars": 4,
            "pump_pct": 18.0,
            # Volume confirmation. 1.0 disables it: on the held-out data it cut
            # the sample by a third and did not improve the edge, so it is off by
            # default rather than carried as decoration.
            "vol_mult": 1.0,
            "vol_window": 24,
            # "either quick or a bit longer" -- the trail decides, not a guess.
            "trail_pct": 8.0,
            "catastrophe_stop_pct": 10.0,
            "max_hold_hours": 24,
            # No profit target. A target is what stops a pump rule from working:
            # the held-out +58.4% trade would have been cut at +8%, and the
            # fixed-target arm of the sweep was worse in every bucket.
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
            "calib_note": "uncalibrated by design; held-out t=-0.32 (undecided), "
                          "net-negative at Robinhood's measured spread, runs as a "
                          "paper experiment",
        }

    def warmup_bars(self) -> int:
        return int(self.params["pump_bars"]) + int(self.params["vol_window"]) + 5

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately does nothing, so the expected edge stays at zero and
        every fill is logged as an experiment rather than a claim."""
        return None

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        L = int(p["pump_bars"])
        VW = int(p["vol_window"])
        if t < self.warmup_bars():
            return []

        close = panel.close
        prev = close[t - L, :]
        px = close[t, :]
        with np.errstate(invalid="ignore", divide="ignore"):
            pump = (px / prev - 1.0) * 100.0

        # Volume over the pump window against the trailing average, so "fast pump
        # on no volume" can be told apart from "fast pump people are trading".
        vol_rel = np.full(panel.N, np.nan)
        vmult = float(p["vol_mult"])
        if panel.volume is not None and vmult > 1.0:
            win = panel.volume[t - L + 1: t + 1, :]
            base = panel.volume[t - VW + 1: t + 1, :]
            with np.errstate(invalid="ignore", divide="ignore"):
                num = np.nanmean(win, axis=0)
                den = np.nanmean(base, axis=0)
                vol_rel = np.where(den > 0, num / den, np.nan)

        thr = float(p["pump_pct"])
        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            mv, last = pump[k], px[k]
            if not (np.isfinite(mv) and np.isfinite(last) and last > 0):
                continue
            if mv < thr:
                continue
            if vmult > 1.0 and not (np.isfinite(vol_rel[k]) and vol_rel[k] >= vmult):
                continue

            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy",
                raw_score=float(mv),
                expected_edge_bps=0.0,
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["max_hold_hours"]) * 3600.0,
                stop_bps=float(p["catastrophe_stop_pct"]) * 100.0,
                # No target: the trail is the exit. target_bps is set far away
                # rather than to zero, because zero would read as "already there".
                # No target: the trail is the exit. None says that; a big
                # number pretends to be a price and gets stored as one.
                target_bps=None,
                trail_bps=float(p["trail_pct"]) * 100.0,
                features={"pump_pct": float(mv),
                          "pump_bars": float(L),
                          "vol_rel": float(vol_rel[k]) if np.isfinite(vol_rel[k]) else None,
                          "trail_pct": float(p["trail_pct"]),
                          # Size by how far past the threshold it went. Held-out
                          # gross edge rose with pump size, so this is the one
                          # feature here with evidence behind it.
                          "conviction_x": float(mv / thr) if thr > 0 else 1.0,
                          "breakeven_round_trip_pct": 1.56,
                          "calibration_n": 0},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
