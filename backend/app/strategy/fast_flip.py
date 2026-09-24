"""S1 -- 'buy anything going up, get out in 5-30 minutes.'

This is the operator's first raw idea, and it is included deliberately as the
CONTROL. It is the strategy most likely to lose to Robinhood's embedded spread,
and running it honestly is the cheapest way to demonstrate that, per trade,
rather than argue about it.

If it turns out to work after costs, that is a genuine finding. If it does not,
the cost-sensitivity curve will show exactly how far away from working it is,
which is far more useful than being told "scalping doesn't work".

Signal: short-horizon momentum (the coin is up over the last L bars) with a
volatility band and a spread filter. Exit on target, stop, or time.
"""
from __future__ import annotations

import numpy as np

from app.strategy._fitting import fit_slope
from app.strategy.base import Panel, Signal, Strategy, lookback_return, realised_vol_bps


class FastFlip(Strategy):
    name = "fast_flip"
    version = "0.1"
    card = "fast_flip"

    @staticmethod
    def defaults() -> dict:
        return {
            "lookback_bars": 10,        # "it's going up right now"
            "hold_bars": 20,            # 5-30 minutes on 1-minute bars
            "entry_move_bps": 50.0,     # must already be moving to qualify
            "vol_window": 60,
            "min_vol_bps": 8.0,
            "max_vol_bps": 200.0,
            "stop_bps": 120.0,
            "target_bps": 150.0,
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "beta_bps_per_unit": 0.0,
            "beta_ci_bps": (0.0, 0.0),
            "calib_n": 0,
            "calib_note": "not calibrated",
        }

    def warmup_bars(self) -> int:
        return int(self.params["lookback_bars"] + self.params["vol_window"] + 5)

    def _signal_value(self, panel: Panel, t: int) -> np.ndarray:
        r = lookback_return(panel.close, t, int(self.params["lookback_bars"]))
        return r * 1e4 / max(self.params["entry_move_bps"], 1.0)   # in units of "entry move"

    def calibrate(self, panel: Panel, end_index: int) -> None:
        H = int(self.params["hold_bars"])
        xs, ys = [], []
        start, stop = max(self.warmup_bars(), 1), int(end_index) - H - 1
        step = max(1, (stop - start) // 4000)
        for t in range(start, stop, step):
            x = self._signal_value(panel, t)
            y = lookback_return(panel.close, t + H, H) * 1e4
            m = np.isfinite(x) & np.isfinite(y) & (x > 1.0)
            if m.any():
                xs.append(x[m]); ys.append(y[m])
        if not xs:
            self.params.update(beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0,
                               calib_note="no usable training observations")
            return
        fit = fit_slope(np.concatenate(xs), np.concatenate(ys))
        self.params.update(beta_bps_per_unit=fit["beta"], beta_ci_bps=fit["ci"],
                           calib_n=fit["n"], calib_note=fit["reason"])

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        x = self._signal_value(panel, t)
        vol = realised_vol_bps(panel.close[: t + 1], int(p["vol_window"]))
        out = []
        for i, sym in enumerate(panel.symbols):
            if not (np.isfinite(x[i]) and np.isfinite(vol[i])):
                continue
            if x[i] < 1.0:                                  # not moving up enough
                continue
            if not (p["min_vol_bps"] <= vol[i] <= p["max_vol_bps"]):
                continue
            edge = float(p["beta_bps_per_unit"]) * float(x[i])
            lo, hi = p["beta_ci_bps"]
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(x[i]),
                expected_edge_bps=max(edge, 0.0),
                edge_ci_bps=(float(lo) * float(x[i]), float(hi) * float(x[i])),
                hold_seconds=float(p["hold_bars"] * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={"move_bps": float(x[i] * p["entry_move_bps"]),
                          "realised_vol_bps": float(vol[i]),
                          "calibration_n": int(p["calib_n"])},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
