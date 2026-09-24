"""Tests for two bugs that were dangerous because they were SILENT.

Neither of these raised, logged an error, or showed on a dashboard. One left
the process running with no memory guard for days; the other left the
false-breakout veto trained on 8% of its data. A system whose failures are
quiet is worse than one that crashes, because you keep trusting it.

Both are now covered here so they cannot come back without a red test.
"""
from __future__ import annotations

import ast
import io
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research import breakout as bo        # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"


# ── 1. a route function must never shadow an imported module ────────────────
def test_no_module_level_name_shadows_an_import():
    """`def health()` under `from app.core import health` rebound the module.

    Every later `health.start_watch()` then raised AttributeError into an
    `except Exception: log.warning(...)`, so the out-of-memory watchdog never
    ran, and nothing anywhere said so.
    """
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        imported: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update((a.asname or a.name.split(".")[0]) for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                imported.update((a.asname or a.name) for a in n.names)
        for n in tree.body:                       # module scope only
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if n.name in imported:
                    offenders.append(f"{path.name}:{n.lineno} def {n.name}")
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id in imported:
                        offenders.append(f"{path.name}:{n.lineno} {t.id} = ...")
    assert not offenders, "module-level names shadow imports: " + "; ".join(offenders)


def test_watch_alive_reports_the_truth_not_the_intention():
    from app.core import health
    health._state["watch"] = True
    health._state["thread"] = None
    assert health.watch_alive() is False, (
        "watch_alive() must report whether a thread is RUNNING, not whether "
        "start_watch() was called — that distinction is the whole point")


# ── 2. one NaN must not destroy a rolling statistic ─────────────────────────
def _series(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    high = close * (1 + abs(rng.normal(0, 0.001, n)))
    low = close * (1 - abs(rng.normal(0, 0.001, n)))
    vol = abs(rng.normal(1000, 100, n))
    return high, low, close, vol


def test_a_single_nan_does_not_poison_atr_forever():
    """The bug: atr() was a prefix sum over true range, so one NaN made every
    later value NaN. BTC had 27 NaNs in 35,027 bars and produced ZERO breakout
    events — the veto silently trained on 8% of the data it should have."""
    high, low, close, _ = _series()
    clean = bo.atr(high, low, close, window=60)
    holed = high.copy()
    holed[100] = np.nan                                  # one hole, near the start
    got = bo.atr(holed, low, close, window=60)

    tail = got[500:]
    assert np.isfinite(tail).mean() > 0.99, (
        "one NaN at bar 100 left %.1f%% of the series after bar 500 as NaN"
        % (100 * (1 - np.isfinite(tail).mean())))
    ok = np.isfinite(clean[500:]) & np.isfinite(tail)
    assert np.allclose(clean[500:][ok], tail[ok], rtol=0.05)


def test_a_single_nan_does_not_poison_vwap_or_trailing_mean():
    high, low, close, vol = _series()
    holed_v = vol.copy(); holed_v[250] = np.nan
    vw = bo.rolling_vwap(close, holed_v, 60)
    assert np.isfinite(vw[500:]).mean() > 0.99

    x = close.copy(); x[300] = np.nan
    tm = bo._trailing_mean(x, 20)
    assert np.isfinite(tm[400:]).all()


def test_trailing_mean_divides_by_observations_not_window_width():
    """Treating a gap as a real zero drags the mean toward nothing. A window
    that is half missing should return the mean of what is there."""
    x = np.array([10.0, np.nan, 10.0, np.nan, 10.0, 10.0])
    out = bo._trailing_mean(x, 4)
    assert np.isclose(out[-1], 10.0), f"expected 10.0, got {out[-1]}"


def test_rolling_mean_std_is_nan_safe():
    _, _, close, _ = _series(2000)
    holed = close.copy(); holed[50] = np.nan
    mu, sd = bo.rolling_mean_std(holed, 100)
    assert np.isfinite(mu[500:]).mean() > 0.99
    assert np.isfinite(sd[500:]).mean() > 0.99


def test_breakouts_survive_a_realistic_panel_with_holes():
    """End to end: a panel with gaps, as the union of 50 coins always has,
    must still produce events. It produced none."""
    from app.strategy.base import Panel
    n = 3000
    high, low, close, vol = _series(n, seed=7)
    for a in (high, low, close, vol):                    # 0.1% holes, as in prod
        a[np.random.default_rng(3).integers(0, n, n // 1000)] = np.nan
    panel = Panel(symbols=["X"], ts=np.arange(n, dtype=float) * 3600,
                  close=close.reshape(-1, 1), high=high.reshape(-1, 1),
                  low=low.reshape(-1, 1), volume=vol.reshape(-1, 1))
    events = bo.find_breakouts(panel, "X", direction=1)
    assert len(events) > 0, "a panel with 0.1% holes produced no breakouts at all"


# ── 3. a strategy must be run on the bar size it was written for ────────────
def test_every_strategy_declares_its_timeframe():
    """regime_swing's own comment said hold_bars was "hours, not minutes", and
    the engine fed it 1-minute bars anyway — turning a ten-day swing into a
    four-hour trade. Nothing raised. The strategy simply stopped being the
    strategy that was designed, and reported an edge of zero forever."""
    # `build_strategy` is the engine's alias; the registry exports it as `build`.
    # This import has been wrong since the test was written, and the test has
    # never run — pytest was not installed anywhere a gate could reach it, and
    # preflight never invoked it. A broken test is indistinguishable from no test.
    from app.strategy.registry import STRATEGIES, build as build_strategy
    bad = []
    for name in STRATEGIES:
        s = build_strategy(name)
        bs = getattr(s, "bar_seconds", None)
        if not isinstance(bs, int) or bs <= 0:
            bad.append(f"{name}: no usable bar_seconds")
            continue
        hold = s.params.get("hold_bars")
        if hold:
            hours = hold * bs / 3600.0
            if not (0.01 <= hours <= 24 * 60):
                bad.append(f"{name}: a hold of {hold} bars at {bs}s is {hours:.1f}h")
    assert not bad, "; ".join(bad)


def test_regime_swing_is_hourly():
    """Its windows are documented in hours. If this ever reads 60 again, the
    ten-day swing has silently become a four-hour scalp."""
    from app.strategy.regime_swing import RegimeSwing
    assert RegimeSwing.bar_seconds == 3600
    assert RegimeSwing().params["hold_bars"] * 3600 / 86400 == 10.0


def test_a_zero_edge_always_says_why():
    """Three different things used to display as a bare 0: not calibrated,
    calibrated with a confidence interval straddling zero, and a genuinely
    negative edge. Those need to be distinguishable or the dashboard is lying
    by omission."""
    from app.strategy.regime_swing import RegimeSwing
    s = RegimeSwing()
    s.params.update(beta_bps_per_unit=-5.0, calib_n=500,
                    calib_note="slope significantly negative")
    import numpy as np
    from app.strategy.base import Panel
    n = s.warmup_bars() + 50
    rng = np.random.default_rng(1)
    # axis=0 matters: np.cumsum with no axis flattens the (n, 3) array to 1-D,
    # and Panel.close[:t+1, j] then raises IndexError. The test never ran, so the
    # mistake sat here undetected.
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, (n, 3)), axis=0), dtype=float)
    panel = Panel(symbols=["BTC", "A", "B"], ts=np.arange(n, dtype=float) * 3600,
                  close=close, high=close * 1.001, low=close * 0.999,
                  volume=np.ones_like(close))
    for sig in s.generate(panel, n - 1):
        f = sig.features
        assert "calibration_verdict" in f and f["calibration_verdict"]
        assert "raw_edge_bps" in f
        if sig.expected_edge_bps == 0:
            assert f["raw_edge_bps"] <= 0


# ── 4. two modules must never resolve the same path differently ─────────────
def test_credential_path_is_resolved_in_exactly_one_place():
    """The setup page wrote credentials to <project>/secrets/… while the API
    client looked in <project>/backend/secrets/… , because each resolved the
    same relative path against its own idea of the working directory. The file
    existed and the app reported 'not configured'.

    The backend runs with cwd=backend/, so any relative path in settings is
    ambiguous unless one resolver owns it."""
    from app.config import get_settings
    from app.core import setup_ops

    s = get_settings()
    assert s.rh_credentials_file.is_absolute()
    assert setup_ops.credentials_path() == s.rh_credentials_file

    import inspect
    from app.execution import rh_api
    src = inspect.getsource(rh_api.RobinhoodCrypto._from_file)
    assert "rh_credentials_file" in src, (
        "rh_api must use the shared resolver, not build its own path")


def test_no_module_resolves_a_settings_path_by_hand():
    """Catch the next one before it ships: nothing outside config.py should be
    joining a bare settings path onto its own base directory."""
    import io as _io
    import re
    from pathlib import Path as _P
    app = _P(__file__).resolve().parents[1] / "app"
    offenders = []
    for f in sorted(app.rglob("*.py")):
        if f.name == "config.py":
            continue
        text = _io.open(f, encoding="utf-8").read()
        for m in re.finditer(r"Path\(\s*getattr\(\s*(?:self\.)?s(?:elf)?[^)]*rh_token_path", text):
            offenders.append(f"{f.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert not offenders, "resolve via settings properties instead: " + ", ".join(offenders)


# ── 3. a parameter grid must sweep parameters that exist ────────────────────
def test_every_param_grid_key_is_a_real_parameter():
    """A grid key that is not a real parameter sweeps nothing, silently.

    `Strategy.__init__` does `{**self.defaults(), **params}`, so an unknown key
    is accepted without complaint and then never read. The walk-forward reports
    that it searched N configurations, the deflated Sharpe is penalised for N,
    and several of those N were the identical strategy. Nothing raises.

    This nearly happened: the hold fix replaced `sell_at_hour` with `hold_hours`
    in morning_dip, and a grid still naming the old key would have quietly
    searched one configuration four times.
    """
    reg = ast.parse((APP / "strategy" / "registry.py").read_text())
    grids: dict[tuple[str, str], list[str]] = {}
    for node in ast.walk(reg):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for tgt in targets:
                if getattr(tgt, "id", "") in ("PARAM_GRIDS", "RETRAIN_GRIDS") \
                        and isinstance(value, ast.Dict):
                    for k, v in zip(value.keys, value.values):
                        if isinstance(v, ast.Dict):
                            grids[(tgt.id, k.value)] = [
                                kk.value for kk in v.keys
                                if isinstance(kk, ast.Constant)]

    assert grids, "no parameter grids found — the parser is wrong, not the grids"
    stale = []
    for (which, name), keys in sorted(grids.items()):
        path = APP / "strategy" / f"{name}.py"
        if not path.exists():
            stale.append(f"{which}[{name}] has no strategy module")
            continue
        declared: set[str] = set()
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.FunctionDef) and n.name == "defaults":
                for sub in ast.walk(n):
                    if isinstance(sub, ast.Dict):
                        declared |= {kk.value for kk in sub.keys
                                     if isinstance(kk, ast.Constant)
                                     and isinstance(kk.value, str)}
        missing = [k for k in keys if k not in declared]
        if missing:
            stale.append(f"{which}[{name}] sweeps {missing}, which {name}.defaults() "
                         f"does not declare")
    assert not stale, "\n  " + "\n  ".join(stale)


# ── 4. boot must not call a function that does not exist ────────────────────
def test_boot_only_calls_functions_that_exist():
    """`main.py` runs its repairs inside try/except, so a typo is a log line.

    On 2026-09-17 an edit to setup_ops.py deleted `backfill_trade_links_once`
    while rewriting the function next to it. Boot then printed

        WARNING: boot repairs failed: module 'app.core.setup_ops'
                 has no attribute 'backfill_trade_links_once'

    and carried on. The operator only saw it because he happened to scroll
    through the startup output looking for something else. On a fresh database
    that missing call is the difference between every trade being traceable and
    none of them being traceable, and nothing would have failed loudly.

    The except block is correct -- a broken repair should not stop the desk from
    trading. This test is the other half of that bargain: the call has to exist.
    """
    main_src = (APP / "main.py").read_text()
    ops_src = (APP / "core" / "setup_ops.py").read_text()

    defined = {n.name for n in ast.walk(ast.parse(ops_src))
               if isinstance(n, ast.FunctionDef)}
    called = set(re.findall(r"\b_ops2?\.([a-zA-Z_][\w]*)\s*\(", main_src))

    assert called, "no setup_ops calls found in main.py -- the parser is wrong"
    missing = sorted(called - defined)
    assert not missing, (
        f"main.py calls setup_ops.{{{', '.join(missing)}}} at boot, but "
        f"setup_ops.py does not define them. This fails as a swallowed warning, "
        f"not as a crash.")
