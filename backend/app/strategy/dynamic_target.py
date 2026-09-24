"""A target that is re-priced as the trade runs, instead of being set once.

2026-09-23, the operator: "No have target for exit on all strategies. I dont
like the no target. What im trying to see is to have dynamic target pricing
meaning analyze the circumstances at various intervals and change the target
accordingly to maximize profit and minimize loss."

So: there is ALWAYS a target. It moves.

Two forces, and they are the two halves of what he asked for:

  MAXIMISE PROFIT -- while the coin is still climbing, the target is kept ahead
  of the price. It never caps a live run, but it never disappears either: if the
  climb stops, the target is sitting just above and the trade takes it. This is
  the difference from `trend_releases_target`, which deletes the target outright
  and hands the trade to the trailing stop for the rest of its life.

  MINIMISE LOSS -- once the trade is old and NOT climbing, the target comes down
  toward what is actually reachable, so a trade that will never see +6.92%
  settles for what it can get instead of running the clock out. It is floored at
  the covered price (entry plus both spreads), so a re-priced target can never
  ask the trade to close at a loss. Only the stop may do that.

Both directions are bounded. The function is pure: same inputs, same answer, no
clock, no database -- so the lab replay and the live engine call the identical
code. The engine asking a different question from the lab is how a rule gets
deployed that was never actually tested.
"""
from __future__ import annotations

# The target never goes below the price that covers entry + both spreads. A
# target under that is an instruction to book a loss, which is the stop's job.
FLOOR_MARGIN = 1.002


def dynamic_target(entry: float, price: float, covered: float, base_frac: float,
                   age_h: float, *, climbing: bool = False,
                   lead_frac: float = 0.03, decay_after_h: float | None = None,
                   decay_per_h: float = 0.0, atr_frac: float = 0.0,
                   atr_lead_k: float = 0.0,
                   shrink: float | None = None) -> float:
    """The target price for this bar. Always a number, never None.

    entry     -- the fill price, spread included
    price     -- the current price. Use the bar's CLOSE, never its high: a rule
                 priced off the high is reading a number it could not have acted
                 on.
    covered   -- entry / (1 - half_spread): selling here returns the money paid
    base_frac -- the strategy's own target as a fraction of entry (0.0692 for
                 volume_build's +6.92%)
    age_h     -- hours the trade has been open
    climbing  -- the caller's causal trend test. The lab and the engine must pass
                 the SAME test; this function does not invent one.
    lead_frac -- while climbing, how far above the current price the target sits
    decay_after_h / decay_per_h -- when not climbing, start lowering the target
                 after this many hours, by this fraction of entry per hour
    atr_frac / atr_lead_k -- optional: scale the lead by the coin's own bar range
                 instead of a flat percentage, so a quiet coin is not asked for a
                 move it never makes
    """
    if entry <= 0:
        return 0.0
    floor = max(covered * FLOOR_MARGIN, entry)
    target = entry * (1.0 + base_frac)

    if climbing:
        # Keep it ahead of the price. max() so the target only ever RISES while
        # the trend holds -- a target that could fall back onto the price during
        # a climb would exit on the first quiet bar, which is the behaviour this
        # is meant to replace.
        lead = lead_frac
        if atr_lead_k and atr_frac:
            lead = max(lead_frac, atr_lead_k * atr_frac)
        target = max(target, price * (1.0 + lead))
    elif shrink is not None:
        # THE ODDS PATH. `shrink` is how much of the target to give up, decided
        # by how long this trade's chance of reaching it has been poor -- not by
        # what hour it is. See time_budget.still_arrives.
        target = entry * (1.0 + max(0.0, base_frac - float(shrink)))
    elif decay_after_h is not None and age_h > decay_after_h and decay_per_h:
        # Not climbing and getting old: ask for less. Linear in hours, floored at
        # the covered price so this can never turn into a loss-taking rule.
        shrink = decay_per_h * (age_h - decay_after_h)
        target = entry * (1.0 + max(0.0, base_frac - shrink))

    return max(target, floor)
