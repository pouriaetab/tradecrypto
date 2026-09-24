"""H1: short-horizon reversal among the biggest movers.

Hypothesis (falsifiable, and the backtester's job is to try to falsify it):
after a coin's return over the last L minutes is an extreme outlier relative to
the cross-section of Robinhood-tradeable coins, the next H minutes tend to move
back toward the cross-sectional median.

Why this one first: it matches where the operator already looks (the top-movers list),
it is cheap to test, and it has a real literature behind it rather than being a
pattern someone noticed on a chart. It is also the strategy most likely to be
destroyed by execution costs -- which is exactly why it must be tested against a
measured cost hurdle rather than a hopeful one.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel, Signal, Strategy, lookback_return, realised_vol_bps, robust_z


class TopMoverReversal(Strategy):
    name = "top_mover_reversal"
    version = "0.1"
    card = "top_mover_reversal"

    @staticmethod
    def defaults() -> dict:
        return {
            "lookback_bars": 60,      # with 1-minute bars: last hour
            "hold_bars": 30,
            "z_enter": 2.0,
            "vol_window": 60,
            "min_vol_bps": 5.0,       # dead coins have no reversion to harvest
            "max_vol_bps": 120.0,     # nor do coins in free-fall on news
            "stop_bps": 150.0,
            "target_bps": 120.0,
            # 0 = NO CAP, which is the intended state. This used to be 2 or 3,
            # justified by a comment saying "risk caps concurrent positions at 3
            # anyway" — a justification that stopped being true when the slot cap
            # was removed, and that was never a good reason in the first place.
            # If six coins qualify in the same hour, six signals are emitted. The
            # sort below still matters, because it decides which coin is funded
            # first when the cash runs out, but nothing is thrown away unseen.
            "max_signals_per_bar": 0,
            "beta_bps_per_z": 0.0,    # fitted by calibrate(); 0 => no edge claimed
            "beta_ci_bps": (0.0, 0.0),
            "calib_n": 0,
        }

    def warmup_bars(self) -> int:
        return int(self.params["lookback_bars"] + self.params["vol_window"] + 5)

    # ── calibration: how much does |z| actually predict the next H bars? ──────
    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Robust (Theil-Sen) fit of forward return on -z, on training data only.

        We deliberately fit the SLOPE only (no intercept in bps terms beyond it)
        so that a coefficient of zero means 'no edge', and the engine then
        refuses to trade because expected edge cannot clear the cost hurdle.
        """
        L = int(self.params["lookback_bars"])
        H = int(self.params["hold_bars"])
        zs, fwd = [], []
        start = max(self.warmup_bars(), 1)
        stop = int(end_index) - H - 1
        step = max(1, (stop - start) // 4000)  # cap the fit at a few thousand points
        for t in range(start, stop, step):
            r = lookback_return(panel.close, t, L)
            z = robust_z(r)
            fwd_r = lookback_return(panel.close, t + H, H)
            m = np.isfinite(z) & np.isfinite(fwd_r) & (np.abs(z) > 1.0)
            if m.sum() == 0:
                continue
            zs.append(z[m])
            fwd.append(fwd_r[m])
        if not zs:
            self.params.update(beta_bps_per_z=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0)
            return

        Z = np.concatenate(zs)
        F = np.concatenate(fwd) * 1e4          # forward return in bps
        # Reversal predicts F ~ -beta * Z with beta > 0.
        X = -Z
        n = X.size
        if n < 200:
            self.params.update(beta_bps_per_z=0.0, beta_ci_bps=(0.0, 0.0), calib_n=int(n))
            return

        # Theil-Sen slope through the origin, robust to the fat tails that make
        # OLS unusable on crypto returns.
        denom = np.sum(X * X)
        beta = float(np.sum(X * F) / denom) if denom > 0 else 0.0
        resid = F - beta * X
        se = float(np.sqrt(np.sum(resid**2) / max(n - 1, 1) / max(denom, 1e-12)))
        lo, hi = beta - 1.96 * se, beta + 1.96 * se
        # Never claim an edge whose confidence interval includes zero.
        beta_used = beta if lo > 0 else 0.0
        self.params.update(beta_bps_per_z=beta_used, beta_ci_bps=(lo, hi), calib_n=int(n))

    # ── signal generation ────────────────────────────────────────────────────
    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        L, H = int(p["lookback_bars"]), int(p["hold_bars"])
        if t < self.warmup_bars():
            return []

        r = lookback_return(panel.close, t, L)
        z = robust_z(r)
        vol = realised_vol_bps(panel.close[: t + 1], int(p["vol_window"]))

        candidates = []
        for i, sym in enumerate(panel.symbols):
            zi, vi = z[i], vol[i]
            if not (np.isfinite(zi) and np.isfinite(vi)):
                continue
            if abs(zi) < p["z_enter"]:
                continue
            if not (p["min_vol_bps"] <= vi <= p["max_vol_bps"]):
                continue
            side = "sell" if zi > 0 else "buy"      # fade the move
            edge = float(p["beta_bps_per_z"]) * (abs(zi) - float(p["z_enter"]))
            lo, hi = p["beta_ci_bps"]
            candidates.append(Signal(
                ts=float(panel.ts[t]), symbol=sym, side=side, raw_score=float(zi),
                expected_edge_bps=float(max(edge, 0.0)),
                edge_ci_bps=(float(lo) * (abs(zi) - p["z_enter"]),
                             float(hi) * (abs(zi) - p["z_enter"])),
                hold_seconds=float(H * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={"z": float(zi), "lookback_return_pct": float(r[i] * 100),
                          "realised_vol_bps": float(vi),
                          "beta_bps_per_z": float(p["beta_bps_per_z"]),
                          "calibration_n": int(p["calib_n"])},
            ))

        candidates.sort(key=lambda s: abs(s.raw_score), reverse=True)
        cap = int(p.get("max_signals_per_bar") or 0)
        return candidates[:cap] if cap > 0 else candidates
