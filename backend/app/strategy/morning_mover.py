"""The strategy the operator has been describing since the beginning.

Every other strategy in this repo trades a clock: rebalance at a fixed hour, or
flip in and out inside twenty minutes. Neither is what he asked for. What he
asked for, repeatedly, is this:

    "some 1-7 cryptos move every day... find out on the same day from oversold
    or sudden volume or volatility, then get in and stay for some time, 3-4
    hours ideally"

That had never been built. The Day Scan tab finds those coins and shows them,
and then nothing happened -- the scanner was read-only while the engine went on
running twenty-minute scalps. This connects the two.

WHAT THE MEASUREMENTS SAY (all on the hourly panel, 33 tradable coins)

  Entry hour. Sweeping every hour from 01:00 to 21:00 Austin, buying a coin whose
  volume-so-far is >= 2x its normal for that same window:

      buy 03:00 -> +5.17% still available before the day's high, +3% reached 53.4%
      buy 06:00 -> +5.00%, 51.1%
      buy 12:00 -> +3.91%, 42.7%
      buy 17:00 -> +2.87%, 31.3%
      buy 21:00 -> +1.91%, 19.1%

  Monotonic decay across all 21 hours, identical shape in Jun-2024->now and
  Oct-2022->now. The early morning window is real. 01:00-08:00 is the zone.

  Entry filter. Sweeping the volume threshold at 04:00 over 823 days, scoring
  "was the day's best coin among the ones we flagged?":

      vol >= 1.5x -> 8.7 coins/day, best-of-day caught 44.3%, picks avg +4.12% room
      vol >= 2.0x -> 5.5 coins/day, 33.3%, +4.49%
      vol >= 3.0x -> 2.8 coins/day, 20.8%, +4.92%

  Exit. Fixed profit targets were tested at +3%, +5% and +8% and ALL of them did
  worse than simply holding to the end of the day, because they cap the winners
  while the losers run. Trailing stops of 1-3% were tested and were worse still
  -- they sit inside the noise band and get whipsawed. So the exit here is time,
  with a wide disaster stop that exists only to cap a catastrophe, not to trade.

WHAT THE MEASUREMENTS ALSO SAY, AND THIS MUST NOT BE SOFTENED

  Holding to the end of the day from a hot-volume morning entry averaged +1.28%
  gross at the strongest volume bucket. The round trip costs 1.92%. That is
  -0.64% per trade. On the numbers as they stand today, this strategy loses.

  It is here anyway, in PAPER, for the reason the operator gave: you cannot
  learn from a trade you never took. Every fill is logged with its features so
  that in a few weeks there is a real sample to test rather than another
  argument. If the true spread turns out to be nearer 0.3%/side than 0.95% --
  which two independent measurements suggest and which nobody has checked with a
  real fill -- the same trade is +1.0% and the conclusion inverts.

  Do not promote this to live on the strength of a good week.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app.strategy.base import Panel, Signal, Strategy

TZ = ZoneInfo("America/Chicago")


class MorningMover(Strategy):
    name = "morning_mover"
    version = "0.1"
    card = "morning_mover"

    # Hourly bars. The effect lives on the day's clock, not the minute's.
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            "volume_multiple": 1.5,       # 8.7 candidates/day, catches the day's best 44%
            "baseline_days": 14,          # what "normal for this hour of the day" means
            "min_hours_elapsed": 2,       # need enough of the day to judge the pace
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "hold_hours": 12,             # ride the afternoon; exits before the quiet hours
            "disaster_stop_bps": 800.0,   # NOT a trading stop -- a catastrophe cap
            "max_extended_pct": 12.0,     # already up this much? the move is behind us
            "expected_edge_bps": 0.0,     # honest: no demonstrated edge yet
            "calib_n": 0,
            "calib_note": "not calibrated -- collecting live paper fills first",
        }

    def warmup_bars(self) -> int:
        return int(self.params["baseline_days"]) * 24 + 26

    # ---- helpers -------------------------------------------------------
    def _hour_of(self, ts: float) -> int:
        return datetime.fromtimestamp(float(ts), TZ).hour

    def _day_of(self, ts: float) -> str:
        return datetime.fromtimestamp(float(ts), TZ).strftime("%Y%m%d")

    def _volume_pace(self, panel: Panel, t: int) -> np.ndarray:
        """Volume so far today vs. this coin's normal for the same slice of day.

        Compared like-for-like: the first N hours of today against the first N
        hours of each prior day. Comparing a part-day against whole days is how
        you accidentally flag every coin at midnight and none at noon.
        """
        vol = getattr(panel, "volume", None)
        if vol is None:
            return np.full(panel.N, np.nan)
        day = self._day_of(panel.ts[t])
        # bars belonging to today, up to and including t
        i = t
        while i > 0 and self._day_of(panel.ts[i - 1]) == day:
            i -= 1
        hours_in = t - i + 1
        if hours_in < int(self.params["min_hours_elapsed"]):
            return np.full(panel.N, np.nan)
        today = np.nansum(vol[i: t + 1], axis=0)

        prior = []
        j = i - 1
        seen = 0
        while j > hours_in and seen < int(self.params["baseline_days"]):
            d = self._day_of(panel.ts[j])
            k = j
            while k > 0 and self._day_of(panel.ts[k - 1]) == d:
                k -= 1
            if (j - k + 1) >= hours_in:                  # same slice of that day
                prior.append(np.nansum(vol[k: k + hours_in], axis=0))
                seen += 1
            j = k - 1
        if len(prior) < 5:
            return np.full(panel.N, np.nan)
        base = np.nanmedian(np.vstack(prior), axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(base > 0, today / base, np.nan)

    # ---- the strategy --------------------------------------------------
    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Deliberately does nothing.

        Fitting a slope on a handful of morning events would produce a confident
        number from noise, and the engine would then treat that number as an
        edge. The expected edge stays 0 until real paper fills say otherwise,
        which means every signal is taken as an EXPERIMENT and recorded as one.
        """
        self.params.update(calib_n=0,
                           calib_note="uncalibrated by design; expected edge stays 0 "
                                      "until live paper fills provide a sample")

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        # NO CLOCK GATE. There used to be one here (01:00-09:00 Austin) and the
        # measurement behind it was real: volume-led entries decayed monotonically
        # from 01:00 to 21:00. A measured decay still does not justify refusing
        # every other hour -- this repo has twice fitted an hour selection that
        # looked significant and reversed on held-out data. The decay reaches
        # sizing through hour_profile's learned weights, where being wrong costs a
        # slightly mis-sized position instead of a trade that never happened.
        hour = self._hour_of(panel.ts[t])

        pace = self._volume_pace(panel, t)
        # Fire on the CROSSING, not on the state. Without this the same coin
        # qualifies at 01:00, 02:00, 03:00 ... and re-signals every hour of the
        # window -- twenty-odd entries a day in the same handful of coins. One
        # entry per coin per time its volume first goes loud. This is a
        # de-duplication of one signal, not a cap on how many coins may fire.
        prev = self._volume_pace(panel, t - 1) if t > 0 else np.full(panel.N, np.nan)
        close = panel.close
        day = self._day_of(panel.ts[t])
        i = t
        while i > 0 and self._day_of(panel.ts[i - 1]) == day:
            i -= 1
        day_open = close[i]

        out: list[Signal] = []
        for k, sym in enumerate(panel.symbols):
            v = pace[k]
            if not np.isfinite(v) or v < float(p["volume_multiple"]):
                continue
            pv = prev[k]
            if np.isfinite(pv) and pv >= float(p["volume_multiple"]):
                continue                      # already loud last hour; not a fresh cross
            px, op = close[t, k], day_open[k]
            if not (np.isfinite(px) and np.isfinite(op) and op > 0):
                continue
            up_so_far = (px / op - 1.0) * 100.0
            if up_so_far > float(p["max_extended_pct"]):
                continue                      # the move already happened without us
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(v),
                expected_edge_bps=0.0,        # honest: nothing demonstrated yet
                edge_ci_bps=(0.0, 0.0),
                hold_seconds=float(p["hold_hours"]) * 3600.0,
                stop_bps=float(p["disaster_stop_bps"]), target_bps=None,
                features={"volume_vs_normal": float(v),
                          "hour_local": float(hour),
                          "up_so_far_pct": float(up_so_far),
                          "hours_elapsed": float(t - i + 1),
                          "volume_prev_hour": float(pv) if np.isfinite(pv) else -1.0,
                          "calibration_n": 0},
            ))
        # loudest volume first -- that is the only ranking with evidence behind it
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
