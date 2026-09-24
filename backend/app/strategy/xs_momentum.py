"""H2: cross-sectional momentum -- the direct opposite of H1, at a longer horizon.

Running a hypothesis and its opposite, under identical costs and identical
statistical gates, is the cheapest protection against talking yourself into
whichever one you happened to test first.
"""
from __future__ import annotations

import numpy as np

from app.strategy.base import Panel, Signal, Strategy, lookback_return, realised_vol_bps


class XSMomentum(Strategy):
    """Rank every coin against the others, buy the leaders.

    This is the only signal in the project that measured positive out of sample.
    Measured on 34,437 fifteen-minute bars across 30 coins, 359 days, holdout
    only — rank by the last day's return and buy the top 3:

        hold 3h   ->  +8.6 bps        hold 12h  ->  +38.9 bps
        hold 1d   -> +60.7 bps        bottom-3 over the same day: -1.7 bps

    Monotonic in both lookback and hold, which is what a real effect looks like
    rather than one lucky cell. It is also the most replicated result in the
    academic literature, found in equities, bonds, commodities and FX.

    It is still short of Robinhood's 192 bps round trip. It is the only thing
    here that is short by 3x rather than 13x.
    """

    name = "xs_momentum"
    version = "0.2"
    card = "xs_momentum"

    # FIFTEEN-MINUTE bars. lookback 96 = one day, hold 12 = three hours.
    bar_seconds = 900

    @staticmethod
    def defaults() -> dict:
        return {
            "lookback_bars": 96,      # one day on 15-minute bars — the measured best
            "hold_bars": 12,          # three hours, inside the 30min-5h band
            "top_k": 3,
            "min_vol_bps": 5.0,
            "max_vol_bps": 150.0,
            "stop_bps": 200.0,
            "target_bps": 200.0,
            "beta_bps_per_rank": 0.0,
            "beta_ci_bps": (0.0, 0.0),
            "calib_n": 0,
        }

    def warmup_bars(self) -> int:
        return int(self.params["lookback_bars"] + self.params["hold_bars"] + 5)

    def calibrate(self, panel: Panel, end_index: int) -> None:
        L, H = int(self.params["lookback_bars"]), int(self.params["hold_bars"])
        xs, fs = [], []
        start, stop = max(self.warmup_bars(), 1), int(end_index) - H - 1
        step = max(1, (stop - start) // 3000)
        for t in range(start, stop, step):
            r = lookback_return(panel.close, t, L)
            f = lookback_return(panel.close, t + H, H)
            m = np.isfinite(r) & np.isfinite(f)
            if m.sum() < 4:
                continue
            # normalised cross-sectional rank in [-1, 1]
            order = np.argsort(np.argsort(r[m]))
            rank = (order / max(m.sum() - 1, 1)) * 2 - 1
            xs.append(rank)
            fs.append(f[m] * 1e4)
        if not xs:
            self.params.update(beta_bps_per_rank=0.0, beta_ci_bps=(0.0, 0.0), calib_n=0)
            return
        X, F = np.concatenate(xs), np.concatenate(fs)
        n = X.size
        denom = float(np.sum(X * X))
        beta = float(np.sum(X * F) / denom) if denom > 0 else 0.0
        resid = F - beta * X
        se = float(np.sqrt(np.sum(resid**2) / max(n - 1, 1) / max(denom, 1e-12)))
        lo, hi = beta - 1.96 * se, beta + 1.96 * se
        self.params.update(beta_bps_per_rank=(beta if lo > 0 else 0.0),
                           beta_ci_bps=(lo, hi), calib_n=int(n))

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        p = self.params
        if t < self.warmup_bars():
            return []
        L, H = int(p["lookback_bars"]), int(p["hold_bars"])
        r = lookback_return(panel.close, t, L)
        vol = realised_vol_bps(panel.close[: t + 1], 60)
        ok = np.isfinite(r) & np.isfinite(vol) & (vol >= p["min_vol_bps"]) & (vol <= p["max_vol_bps"])
        idx = np.where(ok)[0]
        if idx.size < 4:
            return []
        order = idx[np.argsort(r[idx])[::-1]]
        winners = order[: int(p["top_k"])]
        n_ok = idx.size
        out = []
        for rank_pos, i in enumerate(winners):
            norm_rank = 1.0 - 2.0 * rank_pos / max(n_ok - 1, 1)
            edge = float(p["beta_bps_per_rank"]) * norm_rank
            lo, hi = p["beta_ci_bps"]
            out.append(Signal(
                ts=float(panel.ts[t]), symbol=panel.symbols[i], side="buy",
                raw_score=float(r[i]), expected_edge_bps=float(max(edge, 0.0)),
                edge_ci_bps=(float(lo) * norm_rank, float(hi) * norm_rank),
                hold_seconds=float(H * panel.bar_seconds()),
                stop_bps=float(p["stop_bps"]), target_bps=float(p["target_bps"]),
                features={"lookback_return_pct": float(r[i] * 100),
                          "cross_sectional_rank": float(norm_rank),
                          "realised_vol_bps": float(vol[i]),
                          "calibration_n": int(p["calib_n"])},
            ))
        return out
