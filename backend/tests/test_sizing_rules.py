"""The rule the operator has had to state three times, made executable.

    "again why do you keep making hard caps hard fixed codes, i said randomly 8
     we could do 2 trades or none or 1 or 10 or more within that budget. please
     stop setting hard rule around numeric examples i give"

An instruction that lives only in a conversation gets violated again. These tests
fail if a slot count comes back -- in the config, in the guards, or as a curve
fitted to nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings           # noqa: E402
from app.feedback import sizing               # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_no_slot_cap_is_configured():
    """0 means the cap does not exist, which is the intended state."""
    s = get_settings()
    assert s.max_concurrent_positions == 0, (
        f"a position-count cap is set ({s.max_concurrent_positions}). The number "
        f"of positions is an output of cash and signal strength, not a setting. "
        f"On 2026-09-16 a cap of 2 refused the two best entries of the day.")


def test_position_ceiling_is_a_fraction_not_a_dollar_amount():
    """A dollar ceiling silently becomes a position size when equity changes."""
    s = get_settings()
    assert 0 < s.max_position_frac <= 1.0
    assert abs(s.max_position_usd - s.account_equity * s.max_position_frac) < 1e-6, (
        "max_position_usd must be derived from equity, never typed in")


def test_trade_counter_is_a_backstop_not_a_budget():
    """If the daily counter is reachable by ordinary trading it is a slot cap."""
    s = get_settings()
    assert s.max_trades_per_day >= 100, (
        f"max_trades_per_day={s.max_trades_per_day} is low enough to bind during "
        f"normal trading, which makes it a limit rather than a loop detector")


def test_flat_curve_when_conviction_does_not_predict():
    """The default must be 'same size for every qualifying signal'.

    Random conviction against random outcomes has no relationship, and the fit
    has to say so. A sizing curve that ramps on noise is how a strategy ends up
    betting most of the book on its loudest mistake.
    """
    import random
    random.seed(7)
    samples = [(random.uniform(1.0, 5.0), random.gauss(0, 3)) for _ in range(400)]
    prof = sizing.fit("synthetic_noise", samples)
    assert prof["n"] == 400
    assert abs(prof["slope"]) < abs(prof["raw_slope"]) or prof["raw_slope"] == 0, (
        "the fitted slope was not shrunk at all")
    assert abs(prof["slope"]) < 0.25, (
        f"pure noise produced a slope of {prof['slope']:+.3f} -- the shrinkage is "
        f"not doing its job")


def test_a_real_relationship_survives_shrinkage():
    """Shrinkage must not be so aggressive that a genuine signal is erased."""
    import math
    import random
    random.seed(11)
    samples = [(c, 4.0 * math.log(c) + random.gauss(0, 1))
               for c in (random.uniform(1.0, 6.0) for _ in range(400))]
    prof = sizing.fit("synthetic_real", samples)
    assert prof["slope"] > 1.0, f"a strong real slope was shrunk to {prof['slope']:+.3f}"
    assert prof["t"] > 5


def test_small_samples_never_produce_a_slope():
    prof = sizing.fit("synthetic_tiny", [(1.5, 2.0), (2.0, 3.0), (3.0, 4.0)])
    assert prof["flat"] and prof["slope"] == 0.0
    assert "need" in prof["why"]


def test_shares_stay_within_arithmetic_bounds(writable_db):
    """`writable_db` is not decoration. `share()` asks `signals_per_day()` how
    often the strategy speaks, which opens the database. That call handles an
    empty result (it returns 0.0), but it never gets the chance on a fresh clone:
    `get_conn()` opens read-only with `immutable=1`, which refuses to create a
    missing file, so the connection raises before any query runs.

    Without the fixture this test passes only on a machine that already has a
    database, which is the author's and nobody else's. Found on 2026-09-26 by
    running the suite in a clone that had never been started.
    """
    for c in (None, 1.0, 2.0, 12.0, 1e6):
        share, why = sizing.share("no_such_strategy", c, 0.45)
        assert sizing.SHARE_FLOOR <= share <= sizing.SHARE_CEIL, (c, share)
        assert why


def test_env_does_not_reintroduce_a_slot_count():
    """The .env is where the last three slot caps actually lived."""
    env = (ROOT / ".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("TC_MAX_CONCURRENT_POSITIONS="):
            assert line.endswith("=0"), f"slot cap reintroduced in .env: {line}"
        if line.startswith("TC_MAX_POSITION_USD="):
            raise AssertionError(
                "TC_MAX_POSITION_USD is back. Use TC_MAX_POSITION_FRAC so the "
                "ceiling scales with the account instead of becoming a size.")


# ── no cap, no clock, anywhere ──────────────────────────────────────────────
#
#   "Please make sure there is no cap or hours restriction etc anywhere.
#    whatever i said it was just my observation and very few ones i never fully
#    everysecond observed crypto etc."
#
# The operator's observations are hypotheses worth testing, not rules worth
# enforcing. Three separate caps in this repo started life as an offhand number
# from him and became code. These tests fail if a fourth appears.

import ast as _ast
import re as _re
from conftest import skip_without_history

STRAT = Path(__file__).resolve().parents[1] / "app" / "strategy"
GUARDS = Path(__file__).resolve().parents[1] / "app" / "risk" / "guards.py"


def _defaults_of(path: Path) -> dict:
    """Read a strategy's defaults() without importing it."""
    for node in _ast.walk(_ast.parse(path.read_text())):
        if isinstance(node, _ast.FunctionDef) and node.name == "defaults":
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Dict):
                    out = {}
                    for k, v in zip(sub.keys, sub.values):
                        if isinstance(k, _ast.Constant):
                            try:
                                out[k.value] = _ast.literal_eval(v)
                            except Exception:
                                out[k.value] = None
                    return out
    return {}


def test_no_strategy_caps_signals_per_bar():
    """If six coins qualify in one hour, six signals are emitted.

    This was 2 or 3 everywhere, justified by a comment about the concurrent
    position cap — which no longer exists and was never a good reason. Ranking
    still decides which coin is funded first when cash runs out; nothing is
    thrown away before the desk has seen it.
    """
    offenders = []
    for f in sorted(STRAT.glob("*.py")):
        cap = _defaults_of(f).get("max_signals_per_bar")
        if cap:
            offenders.append(f"{f.stem}: max_signals_per_bar={cap}")
    assert not offenders, "; ".join(offenders)


def test_no_strategy_truncates_its_signal_list():
    """The cap can also come back as a bare slice, bypassing the parameter."""
    offenders = []
    for f in sorted(STRAT.glob("*.py")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if _re.search(r"return\s+\w+\[\s*:", line) and "if cap > 0" not in line:
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, "\n  " + "\n  ".join(offenders)


def test_no_strategy_refuses_a_signal_because_of_the_clock():
    """An hour may weight a signal. It may never veto one.

    An hour gate was fitted once and looked good: the best six training hours
    gave +0.32% held out. The entry rule then changed slightly and the same
    procedure picked a completely different six hours, worth -0.58%. Selecting
    hours on this much data finds the calendar's accidents.
    """
    offenders = []
    for f in sorted(STRAT.glob("*.py")):
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            if not _re.search(r"\bhour\b", line):
                continue
            window = " ".join(lines[i:i + 2])
            if _re.search(r"\bhour\b\s*(<|>|<=|>=|==|!=)", line) and \
                    _re.search(r"(continue|return \[\])", window):
                offenders.append(f"{f.name}:{i+1}: {line.strip()}")
    # The gate can also hide behind a helper. regime_swing had `_hour_ok()`,
    # a tidy boolean that read well and still meant "no signal this hour".
    for f in sorted(STRAT.glob("*.py")):
        lines = f.read_text().splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(("#", "\"\"\"")):
                continue
            if not _re.search(r"self\._\w*hour\w*\(", line):
                continue
            if _re.search(r"(continue|return \[\])", " ".join(lines[i:i + 3])):
                offenders.append(f"{f.name}:{i+1}: {line.strip()} (gate behind a helper)")

    assert not offenders, (
        "an hour comparison leads directly to a skipped signal:\n  "
        + "\n  ".join(offenders))


def test_guards_do_not_refuse_on_a_count_or_a_clock():
    """The pre-trade checks may refuse for cash, risk or venue rules — never for
    how many trades have happened or what time it is."""
    src = GUARDS.read_text()
    banned = {
        "one_move_per_coin_per_day":
            "a coin was limited to one entry a day, which was never measured",
        "trading_hours": "a clock gate in the risk layer",
        "max_signals": "a signal count in the risk layer",
    }
    found = [f"{k}: {why}" for k, why in banned.items()
             if _re.search(rf'chk\(\s*"{k}"', src)]
    assert not found, "; ".join(found)
