"""How far the stop sits from entry, when the target is not always the same.

THE MEASUREMENT THAT CAUSED THIS -- 2026-09-21, 49 closed day_climb trades
--------------------------------------------------------------------------
Conviction (the move divided by the rule's own minimum bar) raises the target:
correlation +0.41, t = 3.08. Real. But the stop was `catastrophe_stop_pct`, a
constant -- 800 bps on all 49 trades, whatever the signal looked like. So a
1.01x signal asked for 3.2% against an 8% stop (0.40:1) and a 2.38x signal
asked for 7.5% against the same 8% stop (0.94:1). The rule was quietly running
two different bets under one name.

The consequence, in the same 49 trades: conviction does not improve the win
rate (60% / 85% / 50% across three bands) but it does scale the outcome both
ways -- among winners conviction -> net is +0.39 (t = 2.35), among losers
-0.35 (t = -1.42). A bigger target with an unchanged stop is a bigger bet, not
a better one.

WHAT THIS DOES
--------------
Moves the stop PART OF THE WAY toward the distance that would keep the
reward:risk ratio the rule was designed with, and refuses to move it far:

    ideal = base_stop * (this target / the rule's floor target)
    stop  = base_stop + damp * (ideal - base_stop)          damp = 0.35
    then clamped to +/- max_move of base_stop                max_move = 0.25

Two deliberate pieces of cowardice, both at the operator's instruction ("be
very slow with it"):

  damp      the stop moves a third of the way to the ratio-preserving distance,
            not all of it. A full move would take an 8% stop to 15% on the
            strongest signals, which is not a stop, it is a hope.
  max_move  it can never be more than a quarter away from the number the rule
            has been running with. Whatever this turns out to be worth, it
            cannot be the reason a single trade goes badly wrong.

At conviction 1.0 -- the ordinary case, and the floor target -- this returns
the base stop EXACTLY. Nothing changes for the trades that are already the
majority. It only bites where the target was already stretched.

THIS IS NOT YET MEASURED. It is a change of shape derived from a measured
problem, which is not the same as a measured improvement. exit_lab is the
judge; until it has replayed this against the fixed-stop control on the closed
book, the honest status is "argued for, not shown".
"""
from __future__ import annotations

DAMP = 0.35
MAX_MOVE = 0.25


def conviction_stop_bps(base_stop_bps: float, target_bps: float | None,
                        floor_target_bps: float | None, *,
                        damp: float = DAMP, max_move: float = MAX_MOVE) -> float:
    """Stop distance in bps for a trade whose target is `target_bps`.

    Returns `base_stop_bps` unchanged whenever the inputs cannot support a
    ratio -- a missing target, a non-positive floor, a non-finite number. A
    stop is the last thing that should be improvised from bad inputs.
    """
    try:
        base = float(base_stop_bps)
    except (TypeError, ValueError):
        return float(base_stop_bps)
    if not (base > 0):
        return base
    if target_bps is None or floor_target_bps is None:
        return base
    try:
        tgt, floor = float(target_bps), float(floor_target_bps)
    except (TypeError, ValueError):
        return base
    if not (tgt > 0) or not (floor > 0):
        return base
    if tgt != tgt or floor != floor:          # NaN
        return base
    ideal = base * (tgt / floor)
    out = base + damp * (ideal - base)
    lo, hi = base * (1.0 - max_move), base * (1.0 + max_move)
    return min(max(out, lo), hi)


# ---------------------------------------------------------------------------
# Can this shape of trade pay for itself at all?
#
# 2026-09-21, burst_catch: 14 trades, gross -$0.46, cost -$13.93, net -$14.39.
# The rule was flat on price and lost the entire amount to the spread. Nothing
# about its exits was broken -- the shape was unpayable before the first fill:
#
#     target +4.00%  ->  win  = 4.00 - 1.90 = +2.10% net
#     stop   -3.00%  ->  loss = 3.00 + 1.90 = -4.90% net
#     breakeven win rate = 4.90 / (2.10 + 4.90) = 70%
#
# Nothing on this desk wins 70% of the time. day_climb, the best rule here,
# wins 69% -- and it is the only one above 45%.
#
# The operator's instinct after watching it was to CUT THE TARGET ("take even
# thirty cents"). That moves the wrong lever: a $0.30 net on a $52 position
# needs a 2.48% gross move, and at a +2.50% target the breakeven win rate goes
# from 70% to 89%. The toll is charged per round trip, so the smaller the
# target, the larger the share of it the spread takes. Fast exits are right;
# small targets are not the way to get them.
#
# Hence this gate. A strategy asks it, at signal time, with the real measured
# spread for THAT coin: what win rate does this shape need, and is that a rate
# anything here has ever achieved? If not, the signal is not emitted -- and the
# reason says so in numbers, so it reads as arithmetic rather than a veto.
MAX_PLAUSIBLE_WIN_RATE = 0.65


def breakeven_win_rate(target_bps: float, stop_bps: float,
                       round_trip_bps: float) -> float | None:
    """Fraction of trades that must hit target for this shape to break even.

    Assumes every trade ends at target or at stop. Real books also exit on
    time, somewhere in between, so this is an approximation -- but it is the
    right approximation for a rule with a hard, short time limit, which is
    exactly where it is used. Returns None when the target cannot clear the
    round trip at all, which is not a high bar to hit, it is no bar.
    """
    win = float(target_bps) - float(round_trip_bps)
    loss = float(stop_bps) + float(round_trip_bps)
    if win <= 0 or loss <= 0:
        return None
    return loss / (win + loss)


def shape_survives_costs(target_bps: float, stop_bps: float, round_trip_bps: float,
                         *, max_win_rate: float = MAX_PLAUSIBLE_WIN_RATE
                         ) -> tuple[bool, str, float | None]:
    """(ok, human reason, breakeven win rate). Never raises."""
    try:
        be = breakeven_win_rate(target_bps, stop_bps, round_trip_bps)
    except (TypeError, ValueError):
        return True, "", None
    if be is None:
        return (False,
                f"a {float(target_bps)/100:.2f}% target does not clear the "
                f"{float(round_trip_bps)/100:.2f}% round trip -- this trade cannot "
                f"win even when it is right", None)
    if be > max_win_rate:
        return (False,
                f"{float(target_bps)/100:.2f}% target against a "
                f"{float(stop_bps)/100:.2f}% stop, net of a "
                f"{float(round_trip_bps)/100:.2f}% round trip, needs to be right "
                f"{be:.0%} of the time to break even. Nothing on this desk wins "
                f"{max_win_rate:.0%}", be)
    return True, "", be
