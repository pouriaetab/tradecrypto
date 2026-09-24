"""burst_catch fires on the first bar after a burst has formed inside the
last hour, not on the spike bar itself, and not on a rise that is hours old."""
import numpy as np

from app.strategy.base import Panel
from app.strategy.burst_catch import BurstCatch, BPH


def _panel(kind: str):
    T = 400 * BPH
    ts = 1_700_000_000.0 + np.arange(T) * 900.0
    close = np.full(T, 100.0)
    vol = np.ones(T)
    if kind == "burst":            # flat, then +1.5%, +1.5%, +0.8% over the last three bars
        close[-3:] = [101.5, 103.0, 103.8]; vol[-4:] = 3.0
    elif kind == "spike":          # the whole move in the last bar
        close[-1] = 106.0; vol[-1] = 12.0
    elif kind == "old":            # +4% that happened two hours ago, flat since
        close[-8:] = 104.0; vol[-8:-4] = 3.0
    elif kind == "no_volume":
        close[-3:] = [101.5, 103.0, 103.8]
    return Panel(symbols=["AAA"], ts=ts, close=close[:, None], high=close[:, None] * 1.001,
                 low=close[:, None] * 0.999, volume=vol[:, None])


def test_fires_on_a_fresh_burst_with_volume():
    p = _panel("burst")
    s = BurstCatch().generate(p, p.T - 1)
    assert [x.symbol for x in s] == ["AAA"]
    assert s[0].target_bps == 400.0 and s[0].hold_seconds == 90 * 60


def test_does_not_chase_the_spike_bar():
    p = _panel("spike")
    assert BurstCatch().generate(p, p.T - 1) == []


def test_a_rise_older_than_an_hour_is_not_a_burst():
    p = _panel("old")
    assert BurstCatch().generate(p, p.T - 1) == []


def test_no_volume_no_signal():
    p = _panel("no_volume")
    assert BurstCatch().generate(p, p.T - 1) == []
