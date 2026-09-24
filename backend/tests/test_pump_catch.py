"""pump_catch is volume_build's trigger on a rolling 60-minute window of
15-minute bars -- the SAME rule on a faster clock, not a different rule.

PENGU, 2026-09-19: the run was 11:39 -> 12:19 and the hourly clock bought at
12:24, the top. This file proves the two strategies agree on the same data
(parity), and that the 15-minute version's hour-denominated parameters really
are hours (scaling), so that research/rolling_entry.py compares clocks and not
rules.
"""
import numpy as np

from app.strategy.base import Panel
from app.strategy.pump_catch import PumpCatch, BPH
from app.strategy.volume_build import VolumeBuild


def _panel_15(spike: bool, hours: int = 400):
    T = hours * BPH
    ts = 1_700_000_000.0 + np.arange(T) * 900.0
    close = np.full(T, 100.0)
    # a real two-day run (+12% over 48h) ending in a 2-hour climb of +4%
    run = 48 * BPH
    close[-run:] = np.linspace(100.0, 108.0, run)
    close[-2 * BPH:] = np.linspace(108.0, 115.0, 2 * BPH)
    vol = np.ones(T)
    if spike:
        vol[-BPH:] = 3.0            # the last 60 minutes: 3x normal
    return Panel(symbols=["AAA"], ts=ts, close=close[:, None],
                 high=close[:, None] * 1.001, low=close[:, None] * 0.999,
                 volume=vol[:, None])


def _aggregate_hourly(p15: Panel) -> Panel:
    T = p15.T // BPH
    ts = p15.ts[::BPH][:T]
    close = p15.close[BPH - 1::BPH][:T]
    vol = p15.volume.reshape(T, BPH, -1).sum(axis=1)
    return Panel(symbols=p15.symbols, ts=ts, close=close,
                 high=close * 1.001, low=close * 0.999, volume=vol)


def test_parameters_are_hours_expressed_in_quarter_hours():
    d, h = PumpCatch.defaults(), VolumeBuild.defaults()
    for k in ("window_bars", "baseline_bars", "climb_bars", "trend_bars"):
        assert d[k] == h[k] * BPH, k
    for k in h:
        if k in ("window_bars", "baseline_bars", "climb_bars", "trend_bars", "calib_note"):
            continue
        assert d[k] == h[k], f"{k}: pump_catch must be volume_build's rule, only faster"
    assert set(d) == set(h)
    assert PumpCatch.bar_seconds == 900 and VolumeBuild.bar_seconds == 3600


def test_same_move_fires_on_both_clocks_and_the_rolling_one_fires_first():
    p15 = _panel_15(spike=True)
    ph = _aggregate_hourly(p15)
    h = VolumeBuild().generate(ph, ph.T - 1)
    assert [s.symbol for s in h] == ["AAA"]
    # The rolling clock buys the CROSSING, which happens inside the hour: the
    # first 15-minute bar where the rolling 60-minute volume clears the bar.
    fired = [(t, sig) for t in range(p15.T - BPH, p15.T)
             for sig in PumpCatch().generate(p15, t)]
    assert fired, "pump_catch missed a move volume_build catches"
    t_first, r0 = fired[0]
    assert r0.symbol == "AAA"
    assert float(p15.ts[t_first]) < float(ph.ts[ph.T - 1]) + 3600.0, "must fire before the hour closes"
    r = [r0]
    # identical exit contract, so the lab compares clocks and nothing else
    assert r[0].target_bps == h[0].target_bps
    assert r[0].stop_bps == h[0].stop_bps
    assert r[0].hold_seconds == h[0].hold_seconds


def test_no_volume_no_signal_on_either_clock():
    p15 = _panel_15(spike=False)
    assert PumpCatch().generate(p15, p15.T - 1) == []
    ph = _aggregate_hourly(p15)
    assert VolumeBuild().generate(ph, ph.T - 1) == []


def test_run_so_far_is_measured_over_24_hours_not_24_bars():
    """The extension gate reads 'the last 24 hours'. On 15-minute bars that is
    96 bars; if it read 24 bars (6 hours) a coin that ran 30% yesterday and is
    flat since would pass as 'not extended'."""
    p15 = _panel_15(spike=True)
    # 30% jump 20 hours ago, flat since (still +12% two-day trend, +4% climb)
    k = 20 * BPH
    p15.close[-k:, 0] *= 1.30
    fired = [sig for t in range(p15.T - BPH, p15.T) for sig in PumpCatch().generate(p15, t)]
    assert fired == [], "extension gate must see the 24h run"


def test_backtester_accepts_no_target_and_ratchets_a_trailing_stop():
    """model_lab reported pump_ride as ERROR every day: the backtester
    multiplied a None target. A trailing stop must also actually trail."""
    import numpy as np
    from app.research import backtest as bt
    from app.strategy.base import Panel, Signal, Strategy

    T = 40
    ts = 1_700_000_000.0 + np.arange(T) * 3600.0
    close = np.full(T, 100.0)
    close[10:20] = np.linspace(100, 130, 10)     # a 30% run
    close[20:] = 110.0                           # then a fall
    panel = Panel(symbols=["AAA"], ts=ts, close=close[:, None], high=close[:, None] * 1.001,
                  low=close[:, None] * 0.999, volume=np.ones((T, 1)))

    class Once(Strategy):
        name = "once"; version = "0"; card = "x"
        def warmup_bars(self): return 1
        def generate(self, panel, t):
            return [Signal(ts=float(panel.ts[t]), symbol="AAA", side="buy", raw_score=1.0,
                           expected_edge_bps=1.0, edge_ci_bps=(0.0, 0.0), hold_seconds=30 * 3600,
                           stop_bps=1000.0, target_bps=None, trail_bps=800.0)] if t == 9 else []

    res = bt.run_backtest(panel, Once(), cost_bps_per_side=0.0, apply_hurdle=False)
    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.exit_reason == "stop" and tr.gross_ret > 0.15, "the trail should lock most of a 30% run"
