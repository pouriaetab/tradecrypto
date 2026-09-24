"""Strategy interface shared by the backtester and the live engine.

The same object produces signals in both. If research and production used
different code paths, the backtest would be measuring something that never
trades -- the most common and most expensive bug in this field.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np

# A strategy whose exit is a trailing stop has NO profit target. Saying so with a
# huge number was a mistake that reached the operator's screen: pump_ride set
# target_bps = 100_000 to mean "never", the engine multiplied it out, and an ARB
# position bought at $0.2245 was stored with a target of $2.4692 — eleven times
# the price, on a coin trading at twenty-two cents. It was not a miscalculation;
# it was a sentinel value written into a field that holds a real price, and every
# screen downstream believed it.
#
# `target_bps=None` means there is no target. Anything at or above this bound is
# treated as None too, so an old strategy or a stored row cannot resurrect it.
NO_TARGET_BPS = 50_000.0


def target_price(entry: float, target_bps: float | None, sign: int = 1) -> float | None:
    """The price a target exit would trigger at, or None when there is no target."""
    if target_bps is None or not (0.0 < float(target_bps) < NO_TARGET_BPS):
        return None
    return float(entry) * (1.0 + sign * float(target_bps) / 1e4)




@dataclass
class Panel:
    """Aligned market data. Rows are timestamps, columns are symbols."""

    symbols: list[str]
    ts: np.ndarray                 # (T,) unix seconds
    close: np.ndarray              # (T, N)
    high: np.ndarray | None = None
    low: np.ndarray | None = None
    volume: np.ndarray | None = None

    @property
    def T(self) -> int:
        return self.close.shape[0]

    @property
    def N(self) -> int:
        return self.close.shape[1]

    def bar_seconds(self) -> float:
        return float(np.median(np.diff(self.ts))) if self.T > 2 else 60.0


@dataclass
class Signal:
    ts: float
    symbol: str
    side: str                       # buy | sell
    raw_score: float
    expected_edge_bps: float
    edge_ci_bps: tuple[float, float] = (float("nan"), float("nan"))
    hold_seconds: float = 1800.0
    stop_bps: float = 150.0
    target_bps: float = 120.0
    # A trailing stop, in bps below the high-water mark since the fill. Zero means
    # a fixed stop at stop_bps, which is what every strategy before pump_ride used.
    # The stop only ever ratchets up: it is max(fixed stop, peak * (1 - trail)).
    trail_bps: float = 0.0
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edge_ci_bps"] = list(self.edge_ci_bps)
        return d


class Strategy:
    name: str = "base"
    version: str = "0"
    card: str = ""                  # model-card key in core.registry

    # The bar size this strategy's parameters were WRITTEN for, in seconds.
    #
    # Every parameter below is counted in bars, so this number is what gives them
    # meaning. regime_swing sets hold_bars=240 and its own comment says "hours,
    # not minutes -- this is the swing": 240 hours is a ten-day hold. Fed
    # 1-minute bars instead, the same 240 becomes a four-hour trade, its 200-bar
    # leader average becomes three hours, and the strategy in production is not
    # the strategy that was designed. That is exactly what was happening.
    #
    # The engine now builds a panel per distinct bar size and hands each strategy
    # the one it asked for.
    bar_seconds: int = 60

    def __init__(self, **params):
        self.params = {**self.defaults(), **params}

    @staticmethod
    def defaults() -> dict:
        return {}

    def warmup_bars(self) -> int:
        return 100

    def calibrate(self, panel: Panel, end_index: int) -> None:
        """Fit any coefficients using ONLY data up to `end_index` (exclusive).

        Walk-forward calls this at the start of each fold. Anything fitted on
        data after end_index is look-ahead bias and invalidates the result.
        """
        return None

    def generate(self, panel: Panel, t: int) -> list[Signal]:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "version": self.version,
                "model_card": self.card, "params": self.params}


# ── shared feature helpers ────────────────────────────────────────────────────
def robust_z(x: np.ndarray) -> np.ndarray:
    """Cross-sectional z-score using median/MAD.

    Mean and standard deviation are useless here: one coin doing +60% would
    define the scale for everything else. MAD*1.4826 is the consistent robust
    estimator of sigma under normality.
    """
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    if ok.sum() < 4:
        return np.full_like(x, np.nan)
    med = np.median(x[ok])
    mad = np.median(np.abs(x[ok] - med))
    scale = 1.4826 * mad
    if scale <= 0:
        return np.full_like(x, np.nan)
    return (x - med) / scale


def realised_vol_bps(close: np.ndarray, window: int) -> np.ndarray:
    """Per-bar realised volatility in bps, from log returns."""
    if close.shape[0] <= window:
        return np.full(close.shape[1], np.nan)
    lr = np.diff(np.log(np.maximum(close[-(window + 1):], 1e-12)), axis=0)
    return np.nanstd(lr, axis=0) * 1e4


def lookback_return(close: np.ndarray, t: int, bars: int) -> np.ndarray:
    if t - bars < 0:
        return np.full(close.shape[1], np.nan)
    prev = close[t - bars]
    cur = close[t]
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(prev > 0, cur / prev - 1.0, np.nan)
