"""Catch the first hour of a burst, take a small target, and be out.

WHERE THIS COMES FROM -- 2026-09-20, 10:30-12:00 Austin
-------------------------------------------------------
Twelve coins ran 3-12% inside an hour (SUI, WLD, DOT, NEAR, ENA, SEI, CRV,
ONDO, WIF, OP, ZRO, POPCAT). The desk bought all twelve between 11:41 and
12:11 -- at the top -- because every rule it had needs a climb that is hours
old before it counts (day_climb: +5% over 4 hours; volume_build / pump_catch:
+3% over 2 hours in a two-day trend; pump_ride: +18% over 4 hours).

Read from the minute bars, from the moment each run began (first minute +3%
above the 09:00 base):

    coin    start   next-60-min high   +2% reached   +4% reached
    NEAR    10:54       +12.5%          10:59          11:03
    SUI     11:06        +8.6%          11:34          11:34
    ENA     11:15        +7.9%          11:22          11:31
    SEI     11:32        +5.6%          11:53          11:55
    ZRO     11:25        +3.7%          12:11          14:36
    OP      11:32        +3.6%          11:56          12:33
    WLD     10:52        +3.5%          11:30          11:59
    DOT     11:34        +3.3%          11:45          --
    ONDO    11:05        +3.1%          11:42          --
    WIF     10:58        +2.7%          11:35          --
    CRV     10:36        +2.1%          11:34          --
    POPCAT  11:29        +1.7%          --             --

Eleven of twelve reached +2% within the hour; six reached +4%. Those are
gross figures against a 1.92% round trip: a +2% target nets nothing, a +4%
target nets ~2% on half of them and the rest exit on time or stop. That is
the shape this rule is written for -- the operator's words: "they should have
been tagged as pump catch with a lower target, so we could just get out very
quickly within minutes".

THE RULE
--------
On 15-minute bars (the fastest clock the engine keeps a panel at for every
tracked coin; see pump_catch for why not 1-minute):

    burst      close is >= burst_pct above the close `base_bars` ago (3 hours)
               AND that rise happened inside the last `within_bars` (60 min):
               the close `within_bars` ago was still under half the burst
    volume     the last `within_bars` carried >= vol_mult x the coin's
               14-day per-bar normal (a burst on no volume is a print, not a move)
    not late   close is not more than `late_pct` above the close one bar ago:
               the bar that IS the spike is the one we do not chase
    exit       target +target_pct gross, stop -stop_pct, and a HARD time limit
               of `hold_minutes` -- a burst that has not paid inside its hour
               is over, whatever it looks like

Every threshold above is a starting point for the grid, not a finding, and
this rule is in the same position as every other one here: expected edge 0,
every fill an experiment, judged by the lab on replay and on real fills.
research/rolling_entry.py will get a burst-vs-day_climb comparison once this
has a week of paper.
"""
from __future__ import annotations

import numpy as np

from app.strategy._stops import shape_survives_costs

from app.strategy.base import Panel, Signal, Strategy

BAR_SECONDS = 900
BPH = 3600 // BAR_SECONDS


class BurstCatch(Strategy):
    name = "burst_catch"
    version = "0.1"
    card = "burst_catch"

    bar_seconds = BAR_SECONDS

    @staticmethod
    def defaults() -> dict:
        return {
            "base_bars": 3 * BPH,          # the price three hours ago
            "within_bars": 1 * BPH,        # the rise has to be inside the last hour
            "burst_pct": 3.0,              # what "a burst" is, from the base
            "late_pct": 4.0,               # skip the bar that is the spike itself
            "baseline_bars": 336 * BPH,    # 14-day per-bar volume normal
            "vol_mult": 1.5,
            "target_pct": 4.0,             # gross; ~2% net at Robinhood's spread
            "stop_pct": 3.0,
            "hold_minutes": 90,
            "max_signals_per_bar": 0,
            "expected_edge_bps": 0.0,
            "calib_n": 0,
            "calib_note": "uncalibrated by design; a paper experiment from the 2026-09-20 burst",
        }

    def warmup_bars(self) -> int:
        return int(self.params["baseline_bars"]) + int(self.params["base_bars"]) + 5

    def calibrate(self, panel: Panel, end_index: int) -> None:
        self.params.update(calib_n=0)

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        close, vol = panel.close, panel.volume
        B, W = int(p["base_bars"]), int(p["within_bars"])
        px, base, mid = close[t, :], close[t - B, :], close[t - W, :]
        with np.errstate(invalid="ignore", divide="ignore"):
            burst = (px / base - 1.0) * 100.0
            before = (mid / base - 1.0) * 100.0
            last_bar = (px / close[t - 1, :] - 1.0) * 100.0
            if vol is not None:
                recent = np.nansum(vol[t - W + 1:t + 1], axis=0) / W
                normal = np.nanmean(vol[t - int(p["baseline_bars"]):t - W + 1], axis=0)
                vrel = np.where(normal > 0, recent / normal, np.nan)
            else:
                vrel = np.full(panel.N, np.nan)
        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            b, b0, lb, v = burst[k], before[k], last_bar[k], vrel[k]
            if not (np.isfinite(b) and np.isfinite(b0) and np.isfinite(lb) and np.isfinite(px[k])):
                continue
            if b < float(p["burst_pct"]):
                continue                              # no burst
            if b0 >= b / 2.0:
                continue                              # the rise is older than an hour
            if lb > float(p["late_pct"]):
                continue                              # this bar IS the spike; do not chase it
            if not np.isfinite(v) or v < float(p["vol_mult"]):
                continue                              # no volume behind it

            # THE COST GATE. 2026-09-21: this rule took 14 trades, finished flat
            # on price (gross -$0.46) and lost $13.93 to the spread. Its shape
            # needed a 70% win rate to break even and it got 21%. That was
            # knowable before the first fill, from three numbers, so it is
            # checked before every fill now -- against the spread measured on
            # THIS coin, not a desk-wide average, because the coins this rule
            # fires on (PEPE, FLOKI, BONK, SHIB, WIF) are the dear end of the
            # book. A rule that cannot pay for its own round trip does not get
            # to find out slowly and expensively.
            try:
                from app.data import rh_spread
                sp = rh_spread.get(sym)
                round_trip_bps = float(sp.get("spread_pct", 0.0)) * 2.0 * 100.0
            except Exception:
                round_trip_bps = 0.0
            if round_trip_bps > 0:
                okc, why, _be = shape_survives_costs(
                    float(p["target_pct"]) * 100.0,
                    float(p["stop_pct"]) * 100.0,
                    round_trip_bps)
                if not okc:
                    self._last_cost_block = f"{sym}: {why}"
                    continue
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(b * v),
                expected_edge_bps=0.0, edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["hold_minutes"]) * 60.0,
                stop_bps=float(p["stop_pct"]) * 100.0,
                target_bps=float(p["target_pct"]) * 100.0,
                features={"burst_pct": float(b), "burst_before_last_hour_pct": float(b0),
                          "last_bar_pct": float(lb), "volume_vs_normal": float(v),
                          "conviction_x": float(b / max(float(p["burst_pct"]), 1e-9)),
                          "round_trip_bps": float(round_trip_bps),
                          "breakeven_win_rate": float(
                              (float(p["stop_pct"]) * 100.0 + round_trip_bps) /
                              max(float(p["target_pct"]) * 100.0 - round_trip_bps
                                  + float(p["stop_pct"]) * 100.0 + round_trip_bps, 1e-9))
                              if round_trip_bps > 0 else 0.0,
                          "calibration_n": 0},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
