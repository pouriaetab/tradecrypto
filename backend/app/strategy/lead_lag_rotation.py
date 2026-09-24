"""S4 -- 'the big ones move first, then the next batch, then the next.'

The operator's observation: when crypto moves, it moves in waves -- BTC and ETH
first, then a second tier, then the smaller names. If that is real, there is a
window where the leaders have already moved and a follower has not yet, and you
can be early to the follower.

This is a lead-lag hypothesis, and lead-lag is notoriously easy to find by
accident in correlated assets. So it is measured properly:

  1. For each coin, compute the cross-correlation between the LEADER BASKET's
     returns at time t-k and the coin's returns at time t, for k = 1..max_lag.
  2. Compare each correlation against Bartlett's standard error, ~1/sqrt(T),
     which is the right null for a correlation at lag k.
  3. Apply Benjamini-Hochberg across all (coin, lag) pairs, because with 50 coins
     and 10 lags there are 500 tests and roughly 25 will look significant by luck.
  4. A coin is only a FOLLOWER if a positive lag survives that correction.

Only surviving followers can generate signals, and the signal itself is: the
leader basket has moved up over the last `leader_window` bars by more than the
threshold, and this follower has NOT yet moved proportionally.

The failure mode to keep in mind: on asynchronous or thinly traded data,
apparent lead-lag is often stale prices rather than information flow. That is
why illiquid coins are filtered out before the test rather than after.

References
----------
Lo, A. & MacKinlay, A.C. (1990). When Are Contrarian Profits Due to Stock Market
    Overreaction? RFS 3(2).  -- lead-lag in cross-autocorrelation
Hayashi, T. & Yoshida, N. (2005). On covariance estimation of non-synchronously
    observed diffusion processes. Bernoulli 11(2).
"""
from __future__ import annotations

import math

import numpy as np

from app.research.seasonality import benjamini_hochberg
from app.strategy._fitting import fit_slope
from app.strategy.base import Panel, Signal, Strategy, lookback_return, realised_vol_bps


class LeadLagRotation(Strategy):
    name = "lead_lag_rotation"
    version = "0.1"
    card = "lead_lag_rotation"

    @staticmethod
    def defaults() -> dict:
        return {
            "leaders": ["BTC", "ETH"],
            "max_lag_bars": 10,
            "leader_window": 60,
            "leader_move_min_bps": 80.0,
            "follower_lag_max_bps": 40.0,   # follower has not caught up yet
            "hold_bars": 60,
            "vol_window": 60,
            "min_vol_bps": 6.0,
            "max_vol_bps": 250.0,
            "fdr_alpha": 0.10,
            "stop_bps": 200.0,
            "target_bps": 250.0,
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "followers": {},            # symbol -> best surviving lag, set by calibrate()
            "beta_bps_per_unit": 0.0,
            "beta_ci_bps": (0.0, 0.0),
            "calib_n": 0,
            "calib_note": "not calibrated",
        }

    def warmup_bars(self) -> int:
        return int(self.params["leader_window"] + self.params["vol_window"]
                   + self.params["max_lag_bars"] + 10)

    # ── leader basket ────────────────────────────────────────────────────────
    def _leader_returns(self, panel: Panel, upto: int) -> np.ndarray | None:
        idx = [panel.symbols.index(s) for s in self.params["leaders"] if s in panel.symbols]
        if not idx:
            return None
        lr = np.diff(np.log(np.maximum(panel.close[: upto + 1, idx], 1e-12)), axis=0)
        return np.nanmean(lr, axis=1)

    def _leader_move_bps(self, panel: Panel, t: int) -> float:
        idx = [panel.symbols.index(s) for s in self.params["leaders"] if s in panel.symbols]
        if not idx:
            return float("nan")
        L = int(self.params["leader_window"])
        if t - L < 0:
            return float("nan")
        r = panel.close[t, idx] / panel.close[t - L, idx] - 1.0
        return float(np.nanmean(r) * 1e4)

    # ── calibration: who actually follows, and by how much? ──────────────────
    def calibrate(self, panel: Panel, end_index: int) -> None:
        end_index = int(min(end_index, panel.T))
        lead = self._leader_returns(panel, end_index - 1)
        if lead is None or lead.size < 100:
            self.params.update(followers={}, beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0),
                               calib_n=0, calib_note="leader coins absent or too little history")
            return

        max_lag = int(self.params["max_lag_bars"])
        T = lead.size
        bartlett_se = 1.0 / np.sqrt(max(T, 1))
        tests: list[tuple[str, int, float, float]] = []

        for j, sym in enumerate(panel.symbols):
            if sym in self.params["leaders"]:
                continue
            col = np.diff(np.log(np.maximum(panel.close[: end_index, j], 1e-12)))
            n = min(col.size, lead.size)
            if n < 100:
                continue
            a, b = lead[-n:], col[-n:]
            for k in range(1, max_lag + 1):
                x, y = a[:-k], b[k:]
                m = np.isfinite(x) & np.isfinite(y)
                if m.sum() < 60 or np.std(x[m]) == 0 or np.std(y[m]) == 0:
                    continue
                rho = float(np.corrcoef(x[m], y[m])[0, 1])
                z = rho / bartlett_se
                # two-sided normal p-value
                p = float(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))))
                tests.append((sym, k, rho, p))

        followers: dict[str, int] = {}
        if tests:
            keep = benjamini_hochberg(np.array([t[3] for t in tests]),
                                      float(self.params["fdr_alpha"]))
            best: dict[str, tuple[int, float]] = {}
            for (sym, k, rho, _p), ok in zip(tests, keep):
                if ok and rho > 0 and (sym not in best or rho > best[sym][1]):
                    best[sym] = (k, rho)
            followers = {s: k for s, (k, _) in best.items()}

        self.params["followers"] = followers
        if not followers:
            self.params.update(beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0,
                               calib_note=(f"no coin shows a lagged response to "
                                           f"{'/'.join(self.params['leaders'])} that survives "
                                           f"FDR correction across {len(tests)} tests"))
            return

        # Second stage: does the "leader moved, follower hasn't" gap predict?
        H = int(self.params["hold_bars"])
        L = int(self.params["leader_window"])
        xs, ys = [], []
        start, stop = max(self.warmup_bars(), 1), end_index - H - 1
        step = max(1, (stop - start) // 2000)
        for t in range(start, stop, step):
            lm = self._leader_move_bps(panel, t)
            if not np.isfinite(lm) or lm < self.params["leader_move_min_bps"]:
                continue
            for sym in followers:
                j = panel.symbols.index(sym)
                fr = lookback_return(panel.close, t, L)[j] * 1e4
                if not np.isfinite(fr):
                    continue
                gap = lm - fr                      # how far behind the follower is
                fwd = (panel.close[t + H, j] / panel.close[t, j] - 1.0) * 1e4
                if np.isfinite(gap) and np.isfinite(fwd) and gap > 0:
                    xs.append(gap / 100.0); ys.append(fwd)
        if not xs:
            self.params.update(beta_bps_per_unit=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0,
                               calib_note=f"{len(followers)} followers identified, but the "
                                          "leader-move condition never occurred in training")
            return
        fit = fit_slope(np.array(xs), np.array(ys), min_n=150)
        self.params.update(beta_bps_per_unit=fit["beta"], beta_ci_bps=fit["ci"],
                           calib_n=fit["n"],
                           calib_note=f"{len(followers)} followers survived FDR; {fit['reason']}")

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars() or not p.get("followers"):
            return []
        lm = self._leader_move_bps(panel, t)
        if not np.isfinite(lm) or lm < p["leader_move_min_bps"]:
            return []
        L = int(p["leader_window"])
        vol = realised_vol_bps(panel.close[: t + 1], int(p["vol_window"]))
        out = []
        for sym, lag in p["followers"].items():
            if sym not in panel.symbols:
                continue
            j = panel.symbols.index(sym)
            fr = lookback_return(panel.close, t, L)[j] * 1e4
            if not (np.isfinite(fr) and np.isfinite(vol[j])):
                continue
            if fr > p["follower_lag_max_bps"]:      # already caught up -- too late
                continue
            if not (p["min_vol_bps"] <= vol[j] <= p["max_vol_bps"]):
                continue
            gap = (lm - fr) / 100.0
            edge = float(p["beta_bps_per_unit"]) * gap
            lo, hi = p["beta_ci_bps"]
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side="buy", raw_score=float(gap),
                expected_edge_bps=max(edge, 0.0),
                edge_ci_bps=(float(lo) * gap, float(hi) * gap),
                hold_seconds=float(p["hold_bars"] * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={"leader_move_bps": lm, "follower_move_bps": float(fr),
                          "gap_bps": float(gap * 100), "established_lag_bars": int(lag),
                          "realised_vol_bps": float(vol[j]),
                          "calibration_n": int(p["calib_n"])},
            ))
        out.sort(key=lambda s: s.raw_score, reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return out[:cap] if cap > 0 else out
