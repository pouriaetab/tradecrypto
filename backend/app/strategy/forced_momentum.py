"""S2 -- 'hold longer when the move is being forced up.'

The operator's intuition is that some up-moves are grinding and one-directional
('forced') while others are noise that happens to be green. That distinction is
real and it has a standard measurement: Kaufman's Efficiency Ratio.

    ER = |P_t - P_{t-N}| / sum(|P_k - P_{k-1}|)  over the same N bars

ER near 1 means the move went almost straight there -- every tick of movement
contributed to the net travel. ER near 0 means the coin thrashed around and
ended up where it started. 'Forced' is ER high AND net direction up.

Two confirmations are stacked on top, because efficiency alone is also what a
slow illiquid drift looks like:

  * volume expansion -- the move is being paid for, not drifting on no volume
  * run length -- consecutive higher closes, a crude but honest persistence check

Hold is longer than S1 (the whole point), and the exit is a trailing-style
target/stop plus a time limit.

Reference: Kaufman, P. (1995). Smarter Trading. McGraw-Hill.
"""
from __future__ import annotations

import numpy as np

from app.strategy._fitting import fit_slope
from app.strategy.base import Panel, Signal, Strategy, realised_vol_bps


def efficiency_ratio(close: np.ndarray, t: int, window: int) -> np.ndarray:
    """Signed efficiency ratio: positive when the efficient move was upward."""
    if t - window < 0:
        return np.full(close.shape[1], np.nan)
    seg = close[t - window: t + 1]
    net = seg[-1] - seg[0]
    path = np.nansum(np.abs(np.diff(seg, axis=0)), axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(path > 0, net / path, np.nan)


def run_length(close: np.ndarray, t: int, max_look: int = 20) -> np.ndarray:
    """How many consecutive bars have closed higher."""
    out = np.zeros(close.shape[1])
    for k in range(1, min(max_look, t) + 1):
        up = close[t - k + 1] > close[t - k]
        out += np.where(out == (k - 1), up.astype(float), 0.0)
    return out


class ForcedMomentum(Strategy):
    name = "forced_momentum"
    version = "0.1"
    card = "forced_momentum"

    @staticmethod
    def defaults() -> dict:
        return {
            "er_window": 30,
            "er_min": 0.35,             # below this the move is thrash, not a trend
            "hold_bars": 45,
            "vol_window": 60,
            "min_vol_bps": 8.0,
            "max_vol_bps": 250.0,
            "volume_z_min": 0.5,
            "min_run_length": 2,
            "stop_bps": 180.0,
            "target_bps": 260.0,
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
        return int(self.params["er_window"] + self.params["vol_window"] + 25)

    def _volume_z(self, panel: Panel, t: int, window: int = 60) -> np.ndarray:
        if panel.volume is None or t < window + 1:
            return np.zeros(panel.N)
        v = panel.volume[t - window: t + 1]
        mu, sd = np.nanmean(v[:-1], axis=0), np.nanstd(v[:-1], axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(sd > 0, (v[-1] - mu) / sd, 0.0)

    def _signal_value(self, panel: Panel, t: int) -> np.ndarray:
        er = efficiency_ratio(panel.close, t, int(self.params["er_window"]))
        return np.where(np.isfinite(er), er, np.nan)

    def calibrate(self, panel: Panel, end_index: int) -> None:
        H = int(self.params["hold_bars"])
        xs, ys = [], []
        start, stop = max(self.warmup_bars(), 1), int(end_index) - H - 1
        step = max(1, (stop - start) // 3000)
        for t in range(start, stop, step):
            er = self._signal_value(panel, t)
            fwd = (panel.close[t + H] / panel.close[t] - 1.0) * 1e4
            m = np.isfinite(er) & np.isfinite(fwd) & (er > self.params["er_min"])
            if m.any():
                xs.append(er[m]); ys.append(fwd[m])
        if not xs:
            self.params.update(beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0,
                               calib_note="no bars met the efficiency threshold in training")
            return
        fit = fit_slope(np.concatenate(xs), np.concatenate(ys))
        self.params.update(beta_bps_per_unit=fit["beta"], beta_ci_bps=fit["ci"],
                           calib_n=fit["n"], calib_note=fit["reason"])

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        er = self._signal_value(panel, t)
        vol = realised_vol_bps(panel.close[: t + 1], int(p["vol_window"]))
        vz = self._volume_z(panel, t)
        runs = run_length(panel.close, t)

        out = []
        for i, sym in enumerate(panel.symbols):
            if not (np.isfinite(er[i]) and np.isfinite(vol[i])):
                continue
            if er[i] < p["er_min"]:
                continue
            if not (p["min_vol_bps"] <= vol[i] <= p["max_vol_bps"]):
                continue
            if vz[i] < p["volume_z_min"] or runs[i] < p["min_run_length"]:
                continue
            edge = float(p["beta_bps_per_unit"]) * float(er[i])
            lo, hi = p["beta_ci_bps"]
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(er[i]),
                expected_edge_bps=max(edge, 0.0),
                edge_ci_bps=(float(lo) * float(er[i]), float(hi) * float(er[i])),
                hold_seconds=float(p["hold_bars"] * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={"efficiency_ratio": float(er[i]), "volume_z": float(vz[i]),
                          "run_length": float(runs[i]), "realised_vol_bps": float(vol[i]),
                          "calibration_n": int(p["calib_n"])},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
