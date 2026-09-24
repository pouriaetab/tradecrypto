"""The operator's rule, with no clock in it anywhere.

    "get in when stock is building up then stay until we make go profit then
     get the fuck out"

Three earlier attempts each smuggled a clock in and each one was wrong for it:
`fast_flip` held twenty minutes, `xs_momentum` rebalances on a fixed schedule,
and `morning_mover` only looked between 01:00 and 09:00 -- which would have
missed DOT on 2026-09-08, where volume went to 6.8x at 09:30 and the coin ran
+12% by 10:30. There is no entry hour here and no exit hour. Volume decides when
to buy; profit decides when to sell.

MEASURED, 4 years of hourly bars, 33 tradable coins, 60/40 split by date.

  Entry: volume over the last 4 hours crosses N x its 14-day normal.
  Exit:  the first moment price clears the round trip plus a margin. No clock.

  Louder is better, monotonically:

      2x volume -> 45.9% reach +3% net, average -2.15%
      3x        -> 50.7%,               -1.80%
      5x        -> 56.8%,               -1.33%
      8x        -> 61.3%,               -1.09%     <- default
     12x        -> 56.7%,               -1.92%  (67 trades; noise)

DO NOT BUY A FALLING KNIFE

  His rule, in his words: "dot has been going down, it is so clear, yesterday was
  high overbought and now it is being sold off... do not bet up on a crypto whose
  momentum is down." A coin can satisfy the climb test while bouncing inside a
  decline -- that is the dead cat, and it is the most expensive trade there is.

  Measured over 4 years, same entry, only the trend filter changing:

      no filter (was the default)   72.6% win, 4.0/day, +0.13% gross
      24h return >= +2%             73.4% win, 3.2/day, +0.26%
      48h return >= +2%             75.4% win, 3.0/day, +0.35%   <- default
      48h return >= +5%             76.2% win, 2.4/day, +0.36%

  And the reverse, to check it is the filter doing the work rather than trading
  less: buying ONLY coins whose 48h trend is negative gives 68.6% win and -2.10%
  net -- distinctly worse than everything above. The direction of the bigger
  trend matters, and it matters in the direction he said.

WHY A CLIMB, NOT A SINGLE BAR

  NEAR on 2026-09-09 is the case that settled this. It went +2.67%, +2.00%,
  +1.36%, ... hour after hour from 01:00. A rule needing one explosive bar
  (>=2x volume AND >=2% in that bar) did not fire until 05:00 -- and at 02:00 it
  missed by three thousandths of a percent, on a bar that was 2.03x volume and
  +2.00%. That missed entry was worth +6.2% net; the 05:00 one was worth +1.4%.

  So the test is cumulative: has this coin climbed over the last couple of hours,
  with volume behind it. A steady build counts, which is what "building up"
  actually looks like most of the time.

  Measured, 4 years, 60/40 split, exit at spread + 1%:

      vol 1.5x, +3% over 1h  -> 76.6% win, 2.7 trades/day, +0.60% gross
      vol 1.5x, +3% over 2h  -> 72.6% win, 4.0 trades/day, +0.13% gross
      vol 2.0x, +2% over 1h  -> 69.7% win, 4.1 trades/day, +0.06% gross

  The 2-hour version is the default: it scores slightly worse and it catches the
  NEAR-shaped climb, which the 1-hour version does not. Both are within noise of
  each other and both are gross-positive.

WHY ONE HOUR, NOT FOUR

  On 2026-09-08 both DOT and ADA were profitable trades. Their signatures:

                1-hour volume    4-hour volume    that hour's move
      DOT 09:00     5.58x            2.22x            +4.61%
      ADA 10:00     2.18x            1.50x            +2.98%

  Obvious on the one-hour ratio, invisible on the four-hour. Averaging over four
  hours smooths away the exact spike the rule exists to detect. The window is one
  bar.

  Requiring the price to move in the SAME bar as the volume is what "building up"
  means -- volume without direction is just noise, and it is worth ~0.7% per trade:

      4h window, 5x, no move required   -> -1.15% net, 61.4% win
      4h window, 5x, +2% in that bar    -> -0.77% net, 65.3% win, +1.15% GROSS

THE THRESHOLD CHOICE -- the best-scoring settings are NOT the default

  The best-scoring configuration is a 4-hour window at 5x with a +2% bar move:
  -0.77% net, 65.3% win. It fires 0.7 times a day and would have caught NEITHER
  DOT nor ADA on 2026-09-08.

  The default here is 1 hour / 2.0x / +2%: -1.87% net, 53.3% win, ~3.5 trades a
  day, and it catches both of them. That is a deliberate trade of a better score
  for a rule that fires on the moves the operator is actually pointing at, and
  that produces a sample worth learning from inside a month instead of a year.

  Both are negative at our cost and both are positive gross. Firing rarely does
  not make the cost problem smaller; it makes the sample smaller. The tight
  configuration lives in PARAM_GRIDS so the Model Lab keeps testing it against
  this one on real paper fills, which is the only way this gets settled.

  Stops make it WORSE. Every one of 36 configurations pairing this entry with a
  2/3/4/6% stop was worse than no stop at all, because the stop converts "wait
  and usually recover" into a locked-in loss. Only a catastrophe cap survives.

THE NUMBER THAT MATTERS

  At 8x the rule wins 61.3% of the time and still averages -1.09% per trade.
  The round trip costs 1.92%. So GROSS -- before Robinhood's spread -- this rule
  makes +0.83% per trade.

  It is the first thing in this project that is gross-positive. Almost everything
  else tested was gross-zero or gross-negative, which no venue change could ever
  fix. This one is a cost problem, not a signal problem:

      at 1.92% round trip (Robinhood, as measured) -> -1.09% per trade
      at 0.64% (if the true spread is 0.32%/side)  -> +0.19%
      at 0.40%                                     -> +0.43%

  Which is why measuring the real fill price is worth more than any further
  signal work. Nobody has done it yet.

It runs in PAPER. Expected edge stays 0 so every fill is recorded as an
experiment with its features, and in a few weeks there is a live sample instead
of another backtest.
"""
from __future__ import annotations

import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app.strategy.base import Panel, Signal, Strategy

TZ = ZoneInfo("America/Chicago")     # the operator's trading day
ROUND_TRIP_PCT = 1.9182          # Robinhood, measured on all 33 tradable coins


# THE EXIT -- LET THE BREAKOUT HAPPEN
#
#   The pattern he watches for: something builds after midnight, breaks out, and
#   peaks before noon or in the afternoon. Measured on the 1,364 overnight entries
#   (00:00-06:59) the rule fires on across 4 years:
#
#       best point later that day   avg +7.05%, median +4.39%
#       reaches +3% / +5% / +8%     63% / 45% / 29% of the time
#       hours from entry to high    median 8h  (25-75%: 2h to 17h)
#       the day's high lands        61% before noon, 8% noon-16:00, 30% after
#
#   A +1% target sells in hour one of an eight-hour move. Fifteen exit rules
#   tested on those entries, all after the 1.92% round trip:
#
#       +5% target                  48.5% win   -1.23%   <- best
#       +3% target                  57.1% win   -1.31%
#       +1% target (old default)    71.7% win   -1.47%
#       sell at noon                27.8% win   -1.68%
#       trail 3% once up 3%         26.8% win   -2.16%
#
#   Time exits and trailing stops are worse: they dump losers at noon that were
#   about to recover, and get whipsawed on the way up. A plain target wins. On
#   morning entries +5% and +1% are identical (-1.82 vs -1.81), so +5% is better
#   or equal everywhere.
#
#   Week of 2026-09-01, the 18 overnight entries the rule fires on: +1% target
#   -$44.54, +5% target +$30.98, same entries -- ARB +11.85%, NEAR +12.58%,
#   UNI +14.10% and WLD +12.83% were all sold in hour one by the +1%. One week,
#   not a proof; four years say +0.24%. It lands the right way.
#
#   Give-up time: shortening it only hurts (24h -1.39%, 12h -1.60%) and nothing
#   changes after 36h -- the +5% arrives by then or never. 36h instead of 72h
#   frees the cash a day sooner for the next overnight signal, at zero cost.


def _local_day(ts: float) -> str:
    """Which of the operator's days this bar belongs to."""
    return datetime.fromtimestamp(float(ts), TZ).strftime("%Y%m%d")


class VolumeBuild(Strategy):
    name = "volume_build"
    version = "0.1"
    card = "volume_build"

    bar_seconds = 3600           # the build-up is visible on hours, not minutes

    @staticmethod
    def defaults() -> dict:
        return {
            "window_bars": 1,             # the spike lives in ONE hour -- see below
            "baseline_bars": 336,         # against its own 14-day normal
            "volume_multiple": 1.5,       # see THE THRESHOLD CHOICE in the docstring
            "climb_bars": 2,              # "building up" = climbing for a couple of hours
            "climb_pct": 3.0,             # by at least this much, cumulatively
            "trend_bars": 48,             # the two-day picture -- see U-SHAPE below
            "trend_min_pct": 10.0,        # a genuine run...
            "trend_oversold_pct": -10.0,  # ...or a deep hole it is climbing out of
            "day_min_pct": 0.0,           # and it must not be red TODAY -- see GRAVITY
            "profit_margin_pct": 5.0,     # let the breakout happen -- see THE EXIT
            "give_up_hours": 36,          # nothing changes after 36h -- see THE EXIT
            "catastrophe_stop_pct": 15.0, # never binds in normal trading; caps a disaster
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "max_extended_pct": 25.0,     # if it already ran this far, we are late
            "expected_edge_bps": 0.0,
            "calib_n": 0,
            "calib_note": "uncalibrated by design; runs as an experiment in paper",
        }

    def warmup_bars(self) -> int:
        return int(self.params["baseline_bars"]) + int(self.params["window_bars"]) + 5

    def _volume_ratio(self, panel: Panel, t: int) -> np.ndarray:
        """Volume over the last few hours against this coin's own recent normal."""
        vol = getattr(panel, "volume", None)
        if vol is None:
            return np.full(panel.N, np.nan)
        w = int(self.params["window_bars"])
        b = int(self.params["baseline_bars"])
        if t < b + w:
            return np.full(panel.N, np.nan)
        # A coin with no bars in the window is NaN, not a warning: on the
        # 15-minute panel pump_catch reads, coins without 15-minute history
        # made numpy print "Mean of empty slice" on every tick.
        with np.errstate(divide="ignore", invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            recent = np.nansum(vol[t - w + 1: t + 1], axis=0) / w
            base = np.nanmean(vol[t - b: t - w + 1], axis=0)
            return np.where(base > 0, recent / base, np.nan)

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately no fit.

        Fitting a slope here would hand the engine a confident edge number
        derived from a few hundred events, and it would size on it. The measured
        truth is that this rule is gross-positive and net-negative at our cost;
        that is not something a regression should be allowed to paper over.
        """
        self.params.update(calib_n=0,
                           calib_note="uncalibrated by design; edge stays 0 so every "
                                      "fill is logged as an experiment")

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        ratio = self._volume_ratio(panel, t)
        prev = self._volume_ratio(panel, t - 1) if t > 0 else np.full(panel.N, np.nan)
        thr = float(p["volume_multiple"])

        close = panel.close
        # Everything below is written in HOURS. On a finer panel (pump_catch
        # runs this same rule on 15-minute bars) the bar counts scale, so
        # "the last 24 hours" is still 24 hours and not 24 bars.
        bph = max(1, int(round(3600.0 / float(self.bar_seconds or 3600))))
        lookback = min(t, 24 * bph)

        # first bar of this local day, for the "not red today" test
        today = _local_day(panel.ts[t])
        day_start = t
        while day_start > 0 and _local_day(panel.ts[day_start - 1]) == today:
            day_start -= 1
        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            v = ratio[k]
            if not np.isfinite(v) or v < thr:
                continue
            pv = prev[k]
            if np.isfinite(pv) and pv >= thr:
                continue                       # already loud; buy the crossing, once
            # Volume without direction is noise. The coin has to be CLIMBING --
            # cumulatively over the last few hours, not in one explosive bar.
            cb = int(p["climb_bars"])
            climb = ((close[t, k] / close[t - cb, k] - 1.0) * 100.0
                     if t >= cb and np.isfinite(close[t - cb, k]) and close[t - cb, k] > 0
                     else np.nan)
            if not np.isfinite(climb) or climb < float(p["climb_pct"]):
                continue

            # The bigger trend must be up. A climb inside a decline is a bounce.
            tb = int(p["trend_bars"])
            trend = ((close[t, k] / close[t - tb, k] - 1.0) * 100.0
                     if t >= tb and np.isfinite(close[t - tb, k]) and close[t - tb, k] > 0
                     else np.nan)
            if not np.isfinite(trend):
                continue
            # U-SHAPE. The two-day picture has to be decisive in EITHER
            # direction. Measured with this gate switched OFF, so the entries it
            # had always removed became visible for the first time -- 3,438
            # non-overlapping entries, four years, the 33 tradable coins:
            #
            #   48h trend at entry   trades   avg net   profitable
            #     below -10%            95     -0.09%     62.1%   <- best of all
            #     -10% to -5%          131     -2.08%     36.6%
            #     -5% to 0%            379     -1.40%     44.1%
            #     0% to +2%            299     -2.22%     36.8%
            #     +2% to +5%           590     -2.09%     38.6%
            #     +10% to +20%         814     -1.62%     44.2%
            #     above +20%           226     -1.51%     52.7%
            #
            # A coin bouncing out of a deep hole works, and so does one in a real
            # run. The gentle drift in between is where the money went -- and the
            # old "trend >= +2%" gate sat right on top of the worst bucket.
            #
            # Fitted on three years, read ONCE on the held-out year:
            #   trend <= -10% OR >= +10%   -0.94% over 184 trades, 50.5% profitable
            #   trend >= +2%  (the old)    -1.93% over 489 trades, 39.9% profitable
            #   no gate at all             -2.07% over 704 trades, 38.1% profitable
            # Removing the gate was the WORST option. This is more selective, not
            # less: roughly half as many trades, at half the loss per trade.
            _oversold = float(p.get("trend_oversold_pct", -1e9))
            if not (trend >= float(p["trend_min_pct"]) or trend <= _oversold):
                continue

            # And it must not be red today. A coin can be up strongly over two
            # days and still be falling all morning -- that is the one to skip.
            day_open = close[day_start, k]
            day_move = ((close[t, k] / day_open - 1.0) * 100.0
                        if np.isfinite(day_open) and day_open > 0 else np.nan)
            if not np.isfinite(day_move) or day_move < float(p["day_min_pct"]):
                continue
            px = close[t, k]
            ref = close[t - lookback, k]
            if not (np.isfinite(px) and np.isfinite(ref) and ref > 0):
                continue
            run_so_far = (px / ref - 1.0) * 100.0
            if run_so_far > float(p["max_extended_pct"]):
                continue                       # the move happened without us

            # The target is the round trip plus what we actually want to keep.
            target_pct = ROUND_TRIP_PCT + float(p["profit_margin_pct"])
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(v),
                expected_edge_bps=0.0,
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["give_up_hours"]) * 3600.0,
                stop_bps=float(p["catastrophe_stop_pct"]) * 100.0,
                target_bps=target_pct * 100.0,
                features={"volume_vs_normal": float(v),
                          # Sizing scales on the 48h trend, the feature that
                          # actually separated outcomes over 2,605 entries --
                          # not the volume multiple, which was flat noise.
                          # Conviction from how decisive the two-day move is, in
                          # whichever direction qualified it.
                          "conviction_x": max(
                              1.0,
                              abs(float(trend)) / max(abs(float(p["trend_min_pct"])), 1e-9)
                              if trend >= 0 else
                              abs(float(trend)) / max(abs(_oversold), 1e-9)),
                          "climb_pct": float(climb),
                          "trend_48h_pct": float(trend),
                          "day_move_pct": float(day_move),
                          "volume_prev": float(pv) if np.isfinite(pv) else -1.0,
                          "run_last_24h_pct": float(run_so_far),
                          "keeps_after_costs_pct": float(p["profit_margin_pct"]),
                          "calibration_n": 0},
            ))
        # Rank by the 48-hour trend, not by how loud the volume is.
        #
        # max_signals_per_bar means this sort decides WHICH coins get traded, so
        # it had better rank on something that predicts. Measured over 2,605
        # non-overlapping entries across four years of the 33 tradable coins:
        # the volume multiple is noise (2x -> -1.68%, 3-5x -> -1.65%,
        # 5-10x -> -1.71%, 10x+ -> -1.56%), while the 48h trend is monotonic
        # (2-5% -> -2.06% and 38.5% profitable, rising to 20-40% -> -1.12% and
        # 54.1% profitable, over 672/912/800/207 trades).
        out.sort(key=lambda s: float(s.features.get("trend_48h_pct", 0.0)), reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
