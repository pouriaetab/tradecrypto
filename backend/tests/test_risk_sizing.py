"""Guards against the two failures that produced the ARB position of 2026-09-18.

    "Suddenly the position is over $1k such an outlier … The worst I think is
     that I checked the target and it is over $2!!!! The crypto is around .21ish
     right now! … we have to definitely have so many guards to avoid these
     wiping out my balance when using real money"

Both were real, both were mine, and neither raised anything:

  1. THE TARGET. pump_ride sets no profit target — its exit is a trailing stop.
     It said so with `target_bps = 100_000`, a sentinel meaning "never". The
     engine multiplied it out like any other number: 0.22447 x 11 = 2.4692, a
     target eleven times the price, written into a column that holds real prices.

  2. THE SIZE. Each strategy computed its share of free cash as
     1 / (its own signals per day). pump_ride fires about once a day, so it
     asked for 1/1 — clipped to 95% — and took $1,132 of a $2,000 account. With
     an 8% trailing stop that position risked $90.57 against a $60 daily loss
     cap: one trade able to lose one and a half times the entire day's budget.

The tests below are the bar for anything touching size or exits. The last one is
a property test rather than an example: the invariant is "a position can never
risk more than the desk can afford", and that has to hold for every combination
of price, stop and balance, not for the three someone thought to write down.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategy import base as sbase              # noqa: E402
from app.strategy.registry import ACTIVE_STRATEGIES, build  # noqa: E402
from conftest import skip_without_history


# ── 1. a sentinel must never become a price ─────────────────────────────────

def test_no_target_sentinel_never_becomes_a_price():
    entry = 0.22447405755613564
    assert sbase.target_price(entry, 100_000.0) is None, (
        f"100,000 bps produced a target of {sbase.target_price(entry, 100_000.0)} "
        f"on a coin at ${entry:.4f} — this is the ARB bug")
    assert sbase.target_price(entry, None) is None
    assert sbase.target_price(entry, 0.0) is None
    assert sbase.target_price(entry, -5.0) is None


def test_a_real_target_still_works():
    got = sbase.target_price(100.0, 500.0)          # +5%
    assert got is not None and abs(got - 105.0) < 1e-9


def test_a_target_is_never_absurdly_far_from_the_price():
    """Anything beyond the sentinel bound is refused whatever its value."""
    for bps in (sbase.NO_TARGET_BPS, 60_000.0, 1e9):
        assert sbase.target_price(1.0, bps) is None


def test_every_strategy_declares_a_sane_target():
    """A strategy may have no target. It may not have a preposterous one."""
    bad = []
    for name in ACTIVE_STRATEGIES:
        params = build(name).params
        bps = params.get("expected_edge_bps")
        for key in ("target_bps", "profit_margin_pct"):
            if key in params and params[key] is not None:
                value = float(params[key]) * (100.0 if key.endswith("pct") else 1.0)
                if value >= sbase.NO_TARGET_BPS:
                    bad.append(f"{name}.{key} = {params[key]}")
        del bps
    assert not bad, "; ".join(bad)


# ── 2. size is bounded by what it can lose ──────────────────────────────────

def test_a_rare_strategy_cannot_claim_the_whole_book():
    """The share denominator is the DESK's signal rate, not one strategy's.

    Five strategies each concluding they may have 95% of the cash cannot all be
    right, and the one that fired least got the most.
    """
    skip_without_history()
    from app.feedback import sizing
    shares = [sizing.base_share(name) for name in ACTIVE_STRATEGIES]
    assert max(shares) <= 0.95
    assert sum(shares) <= len(ACTIVE_STRATEGIES) * 0.95
    # With any realistic desk rate no single strategy should be near the ceiling.
    if sizing.desk_signals_per_day() >= 2:
        assert max(shares) < 0.6, (
            f"a single strategy may take {max(shares):.0%} of free cash while the "
            f"desk fires {sizing.desk_signals_per_day():.1f} times a day")


def test_no_history_never_gets_a_bigger_position_than_a_proven_one():
    """The largest position must never sit where the evidence is thinnest.

    This used to assert the MECHANISM of the day -- that `base_share` returned
    SHARE_FLOOR for a strategy with no history -- because `1 / signals per day`
    handed a strategy that fires once a day 1/1 = 95%, and that is how $1,132 of
    a $2,000 account ended up in a single ARB position on 2026-09-18.

    That rule is gone (2026-09-22: it was also shrinking every position as the
    universe grew). base_share is now one flat concentration cap for everybody,
    so there is no ceiling left to hand to an unproven strategy by accident.
    The INVARIANT this test exists for is unchanged, so it is asserted directly
    instead of through a rule that no longer exists.
    """
    from app.feedback import sizing
    assert sizing.base_share("never_fired") == sizing.base_share("day_climb"), \
        "an unproven strategy is being given a different share from a proven one"
    # And the cap alone must never be enough to put the book in one position.
    assert sizing.SHARE_CAP <= 0.5


def test_the_arb_trade_would_be_trimmed_today():
    """The position that prompted this file, re-sized by the rule.

    Re-pointed 2026-09-24: this asserted that $1,132.12 specifically would be
    trimmed. That was true of the book on the day it was written and stopped
    being true when the book shrank -- the test was describing a Tuesday, not a
    rule. The INVARIANT is that a position too big for the remaining risk budget
    is cut, so the size is now derived from the budget instead of typed in.
    """
    skip_without_history()
    from app.feedback import sizing
    from app.risk import guards
    stop = 0.08
    room = max(0.0, guards.risk_budget_remaining_usd("paper")) if hasattr(
        guards, "risk_budget_remaining_usd") else 0.0
    # comfortably past whatever room is left, whatever the book looks like today
    too_big = max(room / stop, 1132.12) * 10 + 1000.0
    allowed, why = sizing.risk_capped_notional(too_big, stop, "paper")
    assert allowed < too_big, "a position past the risk budget was allowed at full size"
    assert why, "a trim must always explain itself"


def test_risk_cap_is_reported_not_silent():
    skip_without_history()
    from app.feedback import sizing
    _, why = sizing.risk_capped_notional(1_000_000.0, 0.08, "paper")
    assert "trimmed" in why


# ── 3. the property that has to hold for every input ────────────────────────

def test_a_position_can_never_risk_more_than_the_daily_cap(monkeypatch):
    """Fuzzed. The invariant is not "these three cases are fine"."""
    from app.feedback import sizing
    from app.risk import guards

    random.seed(20260918)
    for _ in range(300):
        equity = random.uniform(100.0, 250_000.0)
        cap = equity * random.uniform(0.005, 0.05)
        drawdown = equity * random.uniform(0.05, 0.25)
        open_risk = random.uniform(0.0, drawdown)
        stop_frac = random.uniform(0.005, 0.60)
        want = random.uniform(1.0, equity)

        monkeypatch.setattr(guards, "daily_loss_limit_usd", lambda c=cap: c)
        monkeypatch.setattr(guards, "open_risk_usd", lambda _m, r=open_risk: r)
        monkeypatch.setattr(guards, "account",
                            lambda _m, e=equity: {"equity": e, "cash": e, "deployed": 0.0})
        monkeypatch.setattr(sizing, "get_settings",
                            lambda d=drawdown, e=equity: type(
                                "S", (), {"max_drawdown_pct": d / e * 100.0})())

        allowed, _ = sizing.risk_capped_notional(want, stop_frac, "paper")
        risk = allowed * stop_frac

        assert allowed <= want + 1e-6, "sizing may only ever shrink a position"
        assert allowed >= 0.0
        assert risk <= cap + 1e-6, (
            f"a position risking ${risk:,.2f} was allowed against a ${cap:,.2f} cap "
            f"(equity ${equity:,.0f}, stop {stop_frac:.1%})")
        assert open_risk + risk <= drawdown + 1e-6, (
            f"book risk ${open_risk + risk:,.2f} would exceed the ${drawdown:,.2f} "
            f"drawdown limit that halts the desk")


def test_a_missing_stop_is_not_treated_as_a_free_position(monkeypatch):
    """No stop means unknown risk, and unknown risk is never 'none'."""
    from app.feedback import sizing
    allowed, why = sizing.risk_capped_notional(500.0, 0.0, "paper")
    assert allowed == 500.0 and "not capped" in why, (
        "a zero stop distance must be reported, never silently treated as safe")
