"""S3 -- 'when the whole market is alive and going up, get in and hold.'

The operator's observation is that BTC waking up (roughly 60k to 80k over weeks)
drags the rest of the market with it, and that during those stretches the right
trade is to be in early and hold, not to scalp.

This strategy only fires when the REGIME says so, which is the part that makes
it different from S1 and S2. Both regime conditions must hold:

  1. breadth  -- more than `breadth_min` of the universe is above its own trend
  2. leader   -- BTC is above its long moving average with a positive slope

Either alone is not a regime. One coin running is a coin, not a market.

The 'get in early in the morning' part is NOT hard-coded. Hour-of-day
preferences come from `research/seasonality.py`, which tests all 24 hours and
applies false-discovery-rate control, and only hours that SURVIVE that test are
passed in as `allowed_hours`. If none survive, the strategy trades all hours and
says so -- an untested folk pattern does not get to filter trades silently.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from app.data import regime as regime_mod
from app.strategy._fitting import fit_slope
from app.strategy.base import Panel, Signal, Strategy, lookback_return, realised_vol_bps


class RegimeSwing(Strategy):
    name = "regime_swing"
    version = "0.2"
    card = "regime_swing"

    # HOURLY. Every window below is in hours: a 240-bar hold is ten days, the
    # 200-bar leader average is eight days, breadth is measured over five. Run on
    # minute bars these collapse to four hours, three hours and two, which is a
    # different strategy wearing this one's name.
    bar_seconds = 3600

    @staticmethod
    def defaults() -> dict:
        return {
            "breadth_window": 120,
            "breadth_min": 0.60,
            "leader": "BTC",
            "leader_ma_window": 200,
            "leader_slope_bars": 60,
            "entry_lookback": 30,
            "hold_bars": 240,           # hours, not minutes -- this is the swing
            "vol_window": 60,
            "min_vol_bps": 5.0,
            "max_vol_bps": 200.0,
            "stop_bps": 300.0,
            "target_bps": 500.0,
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "allowed_hours": [],        # empty = all hours; filled only by tested survivors
            "tz_offset_hours": -6.0,
            "beta_bps_per_unit": 0.0,
            "beta_ci_bps": (0.0, 0.0),
            "calib_n": 0,
            "calib_note": "not calibrated",
        }

    def warmup_bars(self) -> int:
        p = self.params
        return int(max(p["breadth_window"], p["leader_ma_window"] + p["leader_slope_bars"]) + 10)

    def _regime_ok(self, panel: Panel, t: int) -> tuple[bool, dict]:
        b = regime_mod.breadth(panel, t, int(self.params["breadth_window"]))
        l = regime_mod.leader_trend(panel, t, self.params["leader"],
                                    int(self.params["leader_ma_window"]),
                                    int(self.params["leader_slope_bars"]))
        ok = bool(b.get("value") is not None and b["value"] >= self.params["breadth_min"]
                  and l.get("above_ma") and (l.get("ma_slope_bps") or 0) > 0)
        return ok, {"breadth": b.get("value"), "leader_above_ma": l.get("above_ma"),
                    "leader_slope_bps": l.get("ma_slope_bps")}

    def _hour_weight(self, ts: float) -> float:
        """How much an hour is worth here — never whether it may trade at all.

        `allowed_hours` used to be a veto: outside the surviving hours, no signal.
        It was the best-behaved clock rule in the repo (the hours came from
        seasonality.py with false-discovery-rate control, and an empty list meant
        every hour traded), and it is still being demoted, because the operator's
        rule does not have an exception for well-tested restrictions:

            "Please make sure there is no cap or hours restriction etc anywhere.
             whatever i said it was just my observation"

        A surviving hour now ranks a signal higher; a non-surviving one ranks it
        lower and still trades. Being wrong about the clock costs a slightly
        mis-ranked candidate instead of a trade that never existed.
        """
        allowed = self.params.get("allowed_hours") or []
        if not allowed:
            return 1.0
        h = datetime.fromtimestamp(ts + self.params["tz_offset_hours"] * 3600,
                                   tz=timezone.utc).hour
        return 1.15 if h in allowed else 0.85

    def calibrate(self, panel: Panel, end_index: int) -> None:
        H = int(self.params["hold_bars"])
        L = int(self.params["entry_lookback"])
        xs, ys = [], []
        start, stop = max(self.warmup_bars(), 1), int(end_index) - H - 1
        step = max(1, (stop - start) // 1500)
        for t in range(start, stop, step):
            ok, _ = self._regime_ok(panel, t)
            if not ok:
                continue
            x = lookback_return(panel.close, t, L) * 1e4
            y = (panel.close[t + H] / panel.close[t] - 1.0) * 1e4
            m = np.isfinite(x) & np.isfinite(y)
            if m.any():
                xs.append(np.sign(x[m]) * np.minimum(np.abs(x[m]), 500) / 100.0)
                ys.append(y[m])
        if not xs:
            self.params.update(beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0,
                               calib_note="the risk-on regime never occurred in the training window")
            return
        fit = fit_slope(np.concatenate(xs), np.concatenate(ys), min_n=100)
        self.params.update(beta_bps_per_unit=fit["beta"], beta_ci_bps=fit["ci"],
                           calib_n=fit["n"], calib_note=fit["reason"])

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        ok, detail = self._regime_ok(panel, t)
        if not ok:
            return []
        hour_w = self._hour_weight(float(panel.ts[t]))

        x = lookback_return(panel.close, t, int(p["entry_lookback"])) * 1e4
        vol = realised_vol_bps(panel.close[: t + 1], int(p["vol_window"]))
        out = []
        for i, sym in enumerate(panel.symbols):
            if not (np.isfinite(x[i]) and np.isfinite(vol[i])) or x[i] <= 0:
                continue
            if not (p["min_vol_bps"] <= vol[i] <= p["max_vol_bps"]):
                continue
            unit = float(np.sign(x[i]) * min(abs(x[i]), 500) / 100.0)
            raw_edge = float(p["beta_bps_per_unit"]) * unit
            # Clamping at zero is right for the DECISION -- we never present a
            # predicted loss as an opportunity. But it also made three different
            # situations look identical in the Journal: "not calibrated yet",
            # "calibrated, and the confidence interval straddles zero", and
            # "calibrated, and the edge is negative". The raw number and the
            # calibration's own verdict now travel with the signal.
            edge = max(raw_edge, 0.0)
            lo, hi = p["beta_ci_bps"]
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy",
                raw_score=unit * hour_w,
                expected_edge_bps=max(edge, 0.0),
                edge_ci_bps=(float(lo) * unit, float(hi) * unit),
                hold_seconds=float(p["hold_bars"] * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={**detail, "entry_move_bps": float(x[i]),
                          "realised_vol_bps": float(vol[i]),
                          "hour_weight": hour_w,
                          "hour_filter_active": False,
                          "calibration_n": int(p["calib_n"]),
                          "raw_edge_bps": raw_edge,
                          "edge_clamped_to_zero": bool(raw_edge < 0),
                          "calibration_verdict": str(p.get("calib_note", "")),
                          "bar_seconds": int(panel.bar_seconds())},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
