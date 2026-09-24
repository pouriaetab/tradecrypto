"""What every other exit rule WOULD have returned on the trades we actually took.

    "For every trade that closes, replay the bars it lived through and compute
     what five different exit rules would have returned … Store all five. No
     money at risk, no rule changes."

The point is to settle exit arguments with evidence instead of opinion, without
spending anything to do it. Every closed trade is replayed minute by minute
against every rule; the results accumulate; and when one rule is far enough ahead
to be believed, `research/retrain.py` can promote it through the same
champion-versus-challenger machinery every other change goes through.

WHAT IT DOES NOT DO
-------------------
It does not change how anything trades. It writes rows. Promotion is a separate,
deliberate act with its own evidence bar, because a rule that wins on twenty-seven
trades has won nothing — this project has killed four rules that looked
significant and reversed out of sample.

A NOTE ON HONESTY IN REPLAY
---------------------------
The replay is not free of assumptions and the ones it makes are stated here:

  * Fills are at the bar's price with the full spread charged BOTH sides, the
    same cost model the live engine uses. No rule gets a cheaper fill than the
    desk actually gets.
  * A stop is checked against the bar's LOW, a target against its HIGH. Within a
    single bar that is pessimistic for stops and optimistic for targets, which is
    the honest direction: it never flatters the rule being tested against the one
    running.
  * Minute bars are used when they exist, then 15-minute, then hourly. Coarser
    bars hide intrabar spikes, so a rule's result on hourly data is an estimate
    and is labelled with the granularity it used.
  * Every rule starts from the SAME entry the desk actually took. This measures
    exits only. It cannot tell you the entry was wrong.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from zoneinfo import ZoneInfo

from app.core import clock
from app.core import db
from app.strategy.dynamic_target import dynamic_target as _dyn

SIDE_FALLBACK = 0.0095
MAX_HOLD_H = 24
# How far PAST the hard hold the replay keeps bars, so a rule may be measured on
# whether waiting a few hours longer is worth it. 2026-09-22, the operator:
# "the 24 hours ... should not be a hard limit, because sometimes maybe we are
# very close to getting break even, so if we wait like four or five hours after
# the 24 hours or more, that's okay." A rule that can never see hour 25 cannot
# be measured on hour 25.
GRACE_H = 8
TZ = ZoneInfo("America/Chicago")
# Below this many replayed trades a rule's average is not reported as a finding.
MIN_FOR_A_VERDICT = 40


def ensure_schema() -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS exit_counterfactuals (
            trade_id    INTEGER NOT NULL,
            rule        TEXT NOT NULL,
            net_pct     REAL,
            exit_reason TEXT,
            hours_held  REAL,
            granularity INTEGER,
            computed_at REAL NOT NULL,
            PRIMARY KEY (trade_id, rule)
        )""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_exitcf_rule ON exit_counterfactuals(rule)")


# ─────────────────────────────── the rules ──────────────────────────────────
#
# Each returns (stop_price, target_price_or_None) for the current bar. Returning
# a stop above the current price simply means the next touch exits.

def _tiers(gain: float, table: list[tuple[float, float]], base: float) -> float:
    """Trail width as a function of how far the peak is above entry.

    The operator's idea, made testable:

        "if the high price is good and we think it would drop then selling
         immediately with nice profit would be also option instead of waiting for
         the price to come down to the trailing stop. Or just change the trailing
         stop and set it close to that high"

    A fixed trail treats a trade that is up 1% and one that is up 20% the same
    way, which is what produced an average give-back of 5.07 points across the
    first 27 closed trades — ARB peaked at +17.82% and was exited at +1.69%.
    Tightening as the gain grows keeps more of a big move without strangling a
    small one that still needs room to breathe.
    """
    width = base
    for threshold, w in table:
        if gain >= threshold:
            width = w
    return width


# ── riding a trend instead of trading round it ──────────────────────────────
#
#     "since [it] seem[s] to have the momentum going up and i think we should
#      keep changing and increasing stop loss for these and at time to go above
#      the target rate to squeeze more when it has the potential instead of ...
#      keep doing multiple buy and sell, and just miticicously increase the stop
#      loss and maybe even adjust the time decay and make it less impactful if
#      and only if [it] has a recognizable pattern of up trend"
#
# Three separate behaviours, and the "if and only if" is the whole design:
#
#   1. the stop keeps RISING behind the price          (any trail already does this)
#   2. the fixed target is RELEASED, so a runner runs  (`trend_releases_target`)
#   3. the age-based tightening is SUSPENDED           (`trend_pauses_decay`)
#
# and all of it stops the moment the climb stops, at which point the ordinary
# rule takes back over. The cost of being wrong is bounded by the trail, which
# never widens and never comes back down.
#
# The trend test is deliberately the dullest one that means anything: the coin
# is above its own recent average AND its recent floor is higher than the floor
# before it. Higher lows, in other words. `trend_n` is the window and it is a
# PARAMETER, not a constant, because nobody here knows the right value -- which
# is why every variant below is replayed against the real trades and the number
# decides. No threshold in this file was chosen because it sounded reasonable.

def _mk(trail=None, breakeven=True, decay=None, atr_k=None,
        target=None, hard=0.10, stall_h=None, tiers=None, extend_k=None,
        trend_n=None, trend_releases_target=True, trend_pauses_decay=True,
        trend_trail=None, flat_by_hour=None, bear_h=None,
        progress_h=None, progress_frac=None,
        release_h=None, release_band=None, hard_h=None, grace_h=None,
        dyn_lead=None, dyn_decay_after_h=None, dyn_decay_per_h=0.0,
        dyn_atr_k=0.0):
    def rule(entry, peak, covered, age_h, atr_frac, bars_since_peak_h,
             trending=False, state=None, hour_austin=None, bear=False,
             close=None):
        t = trail
        gain = (peak / entry - 1.0) if entry else 0.0
        riding = bool(trend_n and trending)
        # RELEASING THE TARGET IS A ONE-WAY DOOR, and the first version of this
        # was not. Released while climbing and restored the moment the climb
        # paused, it changed nothing at all: on UNI the target came back three
        # bars later, still below the price, and the trade exited at the same
        # price for the same money. A target that returns is not a target you
        # released -- it is a target you delayed by three bars.
        #
        # Once a trend has said "let this one run", the trade is committed to the
        # trailing stop for the rest of its life. That is the actual bet: a known
        # small profit given up for an unknown larger one, protected by a stop
        # that only ever rises.
        if state is not None:
            if riding and trend_releases_target:
                state["released"] = True
            elif state.get("released"):
                riding = riding or False      # the trail is back to normal...
        released = bool(state and state.get("released"))
        if tiers is not None:
            t = _tiers(gain, tiers, trail)
        if decay is not None and not (riding and trend_pauses_decay):
            # While it is still climbing, age is not evidence against the trade.
            t = max(decay[1], trail - decay[0] * age_h)
        if riding and trend_trail is not None:
            t = trend_trail
        if atr_k is not None and atr_frac:
            t = min(0.25, max(0.03, atr_k * atr_frac))
        stop = peak * (1 - t) if t else entry * (1 - hard)
        if breakeven and peak >= covered * 1.005:
            stop = max(stop, covered)
        stop = max(stop, entry * (1 - hard))
        # "the money is trapped for hours while we could maybe use it for other
        # trades" — a stall exit frees capital that has stopped working.
        if stall_h is not None and bars_since_peak_h >= stall_h and not riding:
            stop = max(stop, 1e18)          # force an exit on this bar
        # THE TIMEOUT LEAK. Measured 2026-09-22 on day_climb's 59 closed trades:
        #
        #     hit target        34 (57%)   +$124.14   avg +$3.65
        #     ran out of time   16 (27%)    -$57.97   avg -$3.62
        #     other exit         8 (13%)    -$17.34
        #     hit stop           2  (3%)    -$12.91
        #
        # The rule makes its money hitting the target and gives back nearly half
        # of it on trades that simply run the clock out. The operator saw it as
        # "getting stuck in positions". This asks whether that is curable: at
        # hour `progress_h`, a trade that has not made `progress_frac` of the way
        # to its target is cut.
        #
        # It is NOT the same as stall_h. A stalled trade has stopped making new
        # highs; this one may be drifting up and still be far too slow to arrive.
        # Whether either works is the replay's call -- both are lab rules, and the
        # day-boundary version of this idea (flat_by_*) already lost money.
        if (progress_h is not None and progress_frac is not None
                and target and age_h >= progress_h and not riding):
            if gain < progress_frac * float(target):
                stop = max(stop, 1e18)      # force an exit on this bar

        # RELEASE THE STUCK ONES. 2026-09-22, the operator: "for ones that we've
        # been in for like many hours, like over 12 hours, 15 hours, and then
        # they have a slight up and then as we are break even or with less loss
        # or a little bit profit, I think we should get out of those -- except
        # the ones that have momentum."
        #
        # So: past `release_h` hours, if the trade has climbed back to within
        # `release_band` of its entry, take it and stop paying for the hope. The
        # `not riding` guard is the exception he asked for -- a trade the trend
        # test says is still going gets left alone.
        if (release_h is not None and release_band is not None
                and age_h >= release_h and not riding):
            if abs(gain) <= release_band:
                stop = max(stop, 1e18)

        # THE SOFT CLOCK. "the 24 hours ... should not be a hard limit ... if we
        # are very close to getting break even, waiting four or five hours after
        # the 24 hours is okay. But if we are clearly seeing the new day cycle
        # for that crypto going down, then just get out."
        #
        # Two halves, and they pull opposite ways on purpose:
        #   past hard_h, a trade within `release_band` of breakeven is GIVEN
        #     grace_h more hours instead of being sold at the clock;
        #   but a trade whose day has turned bear is cut at hard_h regardless.
        # The rule never extends a losing trade that is also falling.
        if hard_h is not None and age_h >= hard_h:
            near = release_band is not None and abs(gain) <= release_band
            if bear or not near or (grace_h is not None and age_h >= hard_h + grace_h):
                stop = max(stop, 1e18)
        # "usually from my observation their behavior restarts in the new day
        #  cycle ... i think is better to close the position before that new day
        #  cycle start". Tested, not assumed: this is a lab rule so the replay
        #  decides, and it is deliberately a flat exit at a wall-clock hour
        #  rather than a stop, because the claim is about the CLOCK.
        if flat_by_hour is not None and hour_austin is not None:
            if hour_austin >= flat_by_hour:
                stop = max(stop, 1e18)
        # "today zec['s] primary trend is down ... so for these is it better to
        #  cut the losses sooner". Tested, not assumed: exit the moment the
        #  coin's own day has the bear shape -- below its day open AND making
        #  lower highs and lower lows over the last `bear_h` hours against the
        #  `bear_h` hours before. Structural, no percentage in it; the window
        #  is the parameter and the replay is the judge.
        if bear_h is not None and bear:
            stop = max(stop, 1e18)
        tgt = entry * (1 + target) if target is not None else None
        # DYNAMIC TARGET (2026-09-23). The operator wants a target at all times
        # that re-prices with conditions, not one number fixed at entry and not
        # the no-target arm. Same function the engine calls -- app.strategy
        # .dynamic_target -- so the lab cannot test something production does
        # not run.
        if dyn_lead is not None and target is not None and close:
            tgt = _dyn(entry, float(close), covered, float(target), age_h,
                       climbing=bool(trending),
                       lead_frac=dyn_lead,
                       decay_after_h=dyn_decay_after_h,
                       decay_per_h=dyn_decay_per_h,
                       atr_frac=atr_frac, atr_lead_k=dyn_atr_k)
        if (riding or released) and trend_releases_target and dyn_lead is None:
            # Let it run. The stop is still underneath it and still rising, so
            # this gives up a known small profit for an unknown larger one --
            # which is a bet, and the replay below is what says whether it pays.
            tgt = None
        # Take profit into strength: sell once the move is far enough beyond what
        # this coin normally does in a bar that it is more likely to be a spike
        # than a trend. No waiting for a decline.
        if extend_k is not None and atr_frac:
            reach = entry * (1 + extend_k * atr_frac)
            tgt = min(tgt, reach) if tgt is not None else reach
        return stop, tgt
    return rule


RULES: dict[str, dict] = {
    # NOT "running today" for most of the book. Only pump_ride asks for a
    # trailing stop; volume_build and day_climb -- every open position on
    # 2026-09-19 -- use a fixed target, a catastrophe stop and a time limit,
    # and never enter the ratchet. The row every rule is compared against is
    # therefore "as_traded" below: what the desk actually booked.
    "trail_8_breakeven":  {"fn": _mk(trail=0.08), "what": "8% trail + breakeven floor (pump_ride's live rule)"},
    "trail_5_breakeven":  {"fn": _mk(trail=0.05), "what": "5% trail + breakeven floor"},
    "trail_12_breakeven": {"fn": _mk(trail=0.12), "what": "12% trail + breakeven floor"},
    "trail_8_no_floor":   {"fn": _mk(trail=0.08, breakeven=False), "what": "8% trail, no floor (the old rule)"},
    "trail_decay_15_5":   {"fn": _mk(trail=0.15, decay=(0.01, 0.05)), "what": "trail tightens 15%→5% as the trade ages"},
    "trail_vol_scaled":   {"fn": _mk(trail=0.08, atr_k=3.0), "what": "trail scaled to the coin's own volatility"},
    "target_8_stop_10":   {"fn": _mk(trail=None, target=0.08), "what": "fixed +8% target, −10% stop"},
    "trail_8_stall_6h":   {"fn": _mk(trail=0.08, stall_h=6.0), "what": "8% trail, but give up after 6h with no new high"},
    # breakeven=False, or it is not a hold: with the floor on, this rule exited
    # 29 of 39 trades flat at the first 2% wiggle and called it "hold to close".
    "hold_to_close":      {"fn": _mk(trail=None, hard=1.0, breakeven=False), "what": "no exit rule — hold the full 24h"},
    # The timeout family. Each is day_climb's live shape (8% trail, 8% target)
    # with one cut-if-too-slow rule added, and `cut_off` is the identical shape
    # with none -- so the comparison is the rule, not the shape.
    "cut_off":            {"fn": _mk(trail=0.08, target=0.08),
                           "what": "control for the cut_* family: no progress rule"},

    # The stuck-position family. Same shape as cut_off, so cut_off is the
    # control for these too.
    "release_12h_be":     {"fn": _mk(trail=0.08, target=0.08, release_h=12.0, release_band=0.01),
                           "what": "past 12h, take it if back within 1% of entry (unless still trending)"},
    "release_15h_be":     {"fn": _mk(trail=0.08, target=0.08, release_h=15.0, release_band=0.01),
                           "what": "past 15h, take it if back within 1% of entry"},
    "release_12h_wide":   {"fn": _mk(trail=0.08, target=0.08, release_h=12.0, release_band=0.02),
                           "what": "past 12h, take it if back within 2% of entry"},
    # The soft clock: 24h stops being a wall.
    "clock_hard_24h":     {"fn": _mk(trail=0.08, target=0.08, hard_h=24.0, release_band=0.0, grace_h=0.0),
                           "what": "control for the soft clock: sell at 24h, no exceptions"},
    "clock_soft_24h_4":   {"fn": _mk(trail=0.08, target=0.08, hard_h=24.0, release_band=0.01, grace_h=4.0),
                           "what": "at 24h, give a near-breakeven trade 4 more hours; cut it anyway if the day turned bear"},
    "clock_soft_24h_8":   {"fn": _mk(trail=0.08, target=0.08, hard_h=24.0, release_band=0.01, grace_h=8.0),
                           "what": "at 24h, give a near-breakeven trade 8 more hours; cut it anyway if the day turned bear"},
    "cut_4h_under_25":    {"fn": _mk(trail=0.08, target=0.08, progress_h=4.0, progress_frac=0.25),
                           "what": "cut at 4h if less than a quarter of the way to target"},
    "cut_8h_under_25":    {"fn": _mk(trail=0.08, target=0.08, progress_h=8.0, progress_frac=0.25),
                           "what": "cut at 8h if less than a quarter of the way to target"},
    "cut_8h_under_50":    {"fn": _mk(trail=0.08, target=0.08, progress_h=8.0, progress_frac=0.50),
                           "what": "cut at 8h if less than halfway to target"},
    "cut_12h_under_50":   {"fn": _mk(trail=0.08, target=0.08, progress_h=12.0, progress_frac=0.50),
                           "what": "cut at 12h if less than halfway to target"},
    # ── tighten as it wins, the operator's idea ────────────────────────────
    "trail_tiered":       {"fn": _mk(trail=0.10, tiers=[(0.05, 0.06), (0.10, 0.04), (0.20, 0.03)]),
                           "what": "trail tightens as the gain grows: 10% → 6% at +5% → 4% at +10% → 3% at +20%"},
    "trail_tiered_tight": {"fn": _mk(trail=0.08, tiers=[(0.03, 0.04), (0.08, 0.025), (0.15, 0.02)]),
                           "what": "tightens harder and sooner: 8% → 4% at +3% → 2.5% at +8% → 2% at +15%"},
    # ── sell into strength rather than wait for a fall ─────────────────────
    "take_profit_3x_range": {"fn": _mk(trail=0.08, extend_k=3.0),
                             "what": "sell once the move reaches 3x the coin's own typical bar range"},
    "take_profit_5x_range": {"fn": _mk(trail=0.08, extend_k=5.0),
                             "what": "sell once the move reaches 5x the coin's own typical bar range"},
    # ── ride it while it climbs, the operator's idea ───────────────────────
    #
    # Same trail as the rule running today, so any difference in the numbers is
    # the TREND BEHAVIOUR and not a different trail. Three windows, because the
    # right one is not knowable in advance and the replay is what decides.
    "ride_trend_n3":  {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05), trend_n=3),
                       "trend_n": 3,
                       "what": "+8% target and an ageing trail — but while it is making "
                               "higher lows over 3 bars, the target is released and the "
                               "trail stops tightening"},
    "ride_trend_n6":  {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05), trend_n=6),
                       "trend_n": 6,
                       "what": "same, over a 6-bar window"},
    "ride_trend_n12": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05), trend_n=12),
                       "trend_n": 12,
                       "what": "same, over a 12-bar window"},
    # And the control: identical rule with the trend behaviour switched OFF, so
    # the comparison is against the same rule rather than against a different
    # one. Without this, "riding trends beats the 8% trail" could just mean
    # "a +8% target beats no target".
    "ride_trend_off": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05)),
                       "what": "CONTROL: the same +8% target and ageing trail, never released"},
    # Wider trail while climbing: give a runner more room, tighten when it stops.
    "ride_trend_n6_wide": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                     trend_n=6, trend_trail=0.12),
                           "trend_n": 6,
                           "what": "as ride_trend_n6, but the trail widens to 12% while "
                                   "it is climbing and snaps back to the ageing trail "
                                   "when it stops"},
    # ── the same idea, WITHOUT the breakeven floor ─────────────────────────
    #
    # The first replay of the rules above returned a result identical to their
    # own control, down to the exit counts, which is never a finding -- it means
    # the thing being varied never got to act. The cause was the breakeven floor
    # they inherit by default: `covered` is entry x (1 + the 1.92% round trip),
    # so the moment a coin ticks ~2% up the stop jumps to +2% and the next wiggle
    # ends the trade flat. Every one of them was stopped out long before a trend,
    # a target or an ageing trail could matter. (trail_8_no_floor scoring +1.69%
    # against trail_8_breakeven's -0.80% is the same effect seen from the side.)
    #
    # So these repeat the experiment with the floor off, which is the only way to
    # find out whether riding a trend does anything at all.
    "ride_nofloor_off": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                   breakeven=False),
                         "what": "CONTROL, no breakeven floor: +8% target, ageing trail"},
    "ride_nofloor_n3":  {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                   breakeven=False, trend_n=3),
                         "trend_n": 3, "what": "ride the trend, 3-bar window, no floor"},
    "ride_nofloor_n6":  {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                   breakeven=False, trend_n=6),
                         "trend_n": 6, "what": "ride the trend, 6-bar window, no floor"},
    "ride_nofloor_n12": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                   breakeven=False, trend_n=12),
                         "trend_n": 12, "what": "ride the trend, 12-bar window, no floor"},
    "ride_nofloor_n6_wide": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                       breakeven=False, trend_n=6, trend_trail=0.12),
                             "trend_n": 6,
                             "what": "ride the trend, 6-bar window, trail widens to 12% "
                                     "while climbing, no floor"},
    # ── be flat before the day rolls over, the operator's observation ──────
    #
    #     "usually from my observation their behavior restarts in the new day
    #      cycle, meaning after 12am ... i think is better to close the position
    #      before that new day cycle start"
    #
    # NEAR #48 is the case that prompted it: held through midnight on the 24-hour
    # limit for -$6.74, when a flat exit at 22:00 Austin would have been +$0.21
    # and the best price in the three hours before midnight was +$2.70.
    #
    # One trade is an anecdote. Three cut-off hours, so the replay says which (if
    # any) is real rather than fitting the one that rescues NEAR.
    "flat_by_2100": {"fn": _mk(trail=0.08, flat_by_hour=21),
                     "what": "8% trail, but be flat by 21:00 Austin"},
    "flat_by_2200": {"fn": _mk(trail=0.08, flat_by_hour=22),
                     "what": "8% trail, but be flat by 22:00 Austin"},
    "flat_by_2300": {"fn": _mk(trail=0.08, flat_by_hour=23),
                     "what": "8% trail, but be flat by 23:00 Austin"},
    "flat_by_2200_nofloor": {"fn": _mk(trail=0.08, breakeven=False, flat_by_hour=22),
                             "what": "as flat_by_2200, without the breakeven floor"},
    # ── cut the day when the day is bear, the operator's ZEC observation ───
    #
    #     "my first impression was that today zec momentum trend primary trend
    #      is down bear ... and indeed it keeps going down, so for these is it
    #      better to cut the losses sooner"
    #
    # Bear = below the day's open AND lower highs AND lower lows over the last
    # N hours versus the N hours before. Two windows; no threshold anywhere.
    "bear_cut_3h": {"fn": _mk(trail=0.08, breakeven=False, bear_h=3), "bear_h": 3,
                    "what": "8% trail, no floor; exit when the coin's day turns bear (3h window)"},
    "bear_cut_6h": {"fn": _mk(trail=0.08, breakeven=False, bear_h=6), "bear_h": 6,
                    "what": "8% trail, no floor; exit when the coin's day turns bear (6h window)"},
    # ── the steady-climber plan, in one rule: ride the trend while it makes
    #    higher lows, cut it if the day turns bear, be flat before the day
    #    rolls over ───────────────────────────────────────────────────────
    "ride_n6_bear6_flat22": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                       breakeven=False, trend_n=6, bear_h=6, flat_by_hour=22),
                             "trend_n": 6, "bear_h": 6,
                             "what": "ride while making higher lows (6 bars), cut if the day "
                                     "turns bear (6h), flat by 22:00 Austin, no floor"},
    "ride_n6_flat22": {"fn": _mk(trail=0.08, target=0.08, decay=(0.01, 0.05),
                                 breakeven=False, trend_n=6, flat_by_hour=22),
                       "trend_n": 6,
                       "what": "ride while making higher lows (6 bars), flat by 22:00 Austin, no floor"},
}



# ── scaling out, and the trap it sets ───────────────────────────────────────
#
#     "i had done this manually myself before and i have got stuck partially in
#      very bad positions because it tricked as if the huge drop will go back up
#      but the drop just continued after a solid high"
#
# That is the real failure of scaling out and it is worth naming precisely: you
# sell the part that was winning and keep the part that then loses. Taking money
# off the table FEELS like it has de-risked the trade, so the remainder gets held
# through a decline it would never have been entered into. The profit on the
# first leg becomes permission to be wrong on the second.
#
# The design below removes that permission mechanically:
#
#   1. THE REMAINDER ALWAYS KEEPS A STOP. Selling part of a position never
#      widens, pauses or removes the exit on the rest.
#   2. THE REMAINDER'S STOP JUMPS TO COMBINED BREAKEVEN. Once the first leg is
#      banked, the stop on the rest is moved to whatever price makes the WHOLE
#      trade return what was paid:
#
#          p_stop = (entry/(1-s) - f * p_sold) / (1 - f)
#
#      After that the drop can continue as far as it likes; the trade cannot end
#      below break-even. That is the protection missing from doing it by hand.
#   3. NOTHING IS EVER ADDED TO A LOSER.

def _scale_out(entry, covered, side, bars, t0, fraction, trigger_k, trail, atr_frac):
    """Sell `fraction` when the move reaches trigger_k x the coin's own range,
    then trail the rest behind a stop that can no longer lose overall."""
    if not atr_frac:
        return None
    trigger = entry * (1 + trigger_k * atr_frac)
    peak = entry
    banked = 0.0          # value already realised, per unit of ORIGINAL position
    left = 1.0
    stop = entry * (1 - trail)
    for b in bars:
        age_h = (b["ts"] - t0) / 3600.0
        if b["high"] and b["high"] > peak:
            peak = b["high"]

        # leg 1: take part of it off into strength
        if left > fraction and b["high"] and b["high"] >= trigger:
            banked += fraction * trigger * (1 - side)
            left -= fraction
            # the stop that makes the WHOLE trade breakeven from here
            need = (covered - fraction * trigger) / max(left, 1e-9)
            stop = max(stop, need)

        # the remainder always has a live stop, and it only tightens
        stop = max(stop, peak * (1 - trail))
        if left < 1.0:
            stop = max(stop, (covered - banked) / max(left, 1e-9))

        if b["low"] and b["low"] <= stop:
            px = min(stop, b["high"] or stop)
            total = banked + left * px * (1 - side)
            return total / entry - 1, ("stop after partial" if left < 1.0 else "stop"), age_h

    last = bars[-1]
    total = banked + left * last["close"] * (1 - side)
    return total / entry - 1, "time", (last["ts"] - t0) / 3600.0


# The two hindsight bounds replay() emits alongside the rules. Named so callers
# can account for them instead of carrying a magic "+2" -- and so it is obvious
# in one place that they are NOT in RULES and are not tradeable.
BOUNDS = ("_ceiling_peak", "_floor_trough")
BOUND_WHAT = {
    "_ceiling_peak":  "the best price the coin ever showed while we held it — "
                      "perfect hindsight, nothing can beat this",
    "_floor_trough":  "the worst price it ever showed while we held it — "
                      "perfect bad luck, nothing can be worse than this",
}

# ── THE DYNAMIC TARGET ───────────────────────────────────────────────────────
# 2026-09-23. "I dont like the no target. What im trying to see is to have
# dynamic target pricing meaning analyze the circumstances at various intervals
# and change the target accordingly to maximize profit and minimize loss."
#
# Every arm keeps a target at ALL times. `dyn_control` is the paired control:
# the same +6.92% base, fixed at entry, nothing dynamic. Anything the family
# earns has to be earned against that row, not against hold_to_close.
_BASE = 0.0692           # volume_build's live target: 1.9182% round trip + 5%
_TREND = 6               # the same causal trend window the other families use

RULES.update({
    "dyn_control": {"fn": _mk(target=_BASE, hard=0.10, trend_n=None),
                    "what": "fixed +6.92% target set at entry — the control"},
    "dyn_lead3": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                            trend_releases_target=False, dyn_lead=0.03),
                  "trend_n": _TREND,
                  "what": "target rides 3% above the price while the coin climbs"},
    "dyn_lead6": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                            trend_releases_target=False, dyn_lead=0.06),
                  "trend_n": _TREND,
                  "what": "target rides 6% above the price while the coin climbs"},
    "dyn_decay_only": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                                 trend_releases_target=False, dyn_lead=0.0,
                                 dyn_decay_after_h=8, dyn_decay_per_h=0.004),
                       "trend_n": _TREND,
                       "what": "target falls 0.4%/h after 8h when not climbing, floored at breakeven"},
    "dyn_lead3_decay": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                                  trend_releases_target=False, dyn_lead=0.03,
                                  dyn_decay_after_h=8, dyn_decay_per_h=0.004),
                        "trend_n": _TREND,
                        "what": "rides 3% above a climb, and falls 0.4%/h after 8h when it stalls"},
    "dyn_lead3_decay_fast": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                                       trend_releases_target=False, dyn_lead=0.03,
                                       dyn_decay_after_h=4, dyn_decay_per_h=0.008),
                             "trend_n": _TREND,
                             "what": "same, but starts falling at 4h and twice as fast"},
    "dyn_atr_lead": {"fn": _mk(target=_BASE, hard=0.10, trend_n=_TREND,
                               trend_releases_target=False, dyn_lead=0.02,
                               dyn_atr_k=1.5, dyn_decay_after_h=8,
                               dyn_decay_per_h=0.004),
                     "trend_n": _TREND,
                     "what": "lead scales with the coin's own bar range, not a flat %"},
})

SCALE_RULES = {
    "scale_third_at_3x":  {"f": 1/3, "k": 3.0, "trail": 0.08,
                           "what": "sell a third at 3x the coin's range, rest trails 8% behind a no-loss stop"},
    "scale_half_at_3x":   {"f": 0.5, "k": 3.0, "trail": 0.08,
                           "what": "sell half at 3x the range, rest trails 8% behind a no-loss stop"},
    "scale_third_at_5x":  {"f": 1/3, "k": 5.0, "trail": 0.10,
                           "what": "sell a third at 5x the range, rest trails 10% behind a no-loss stop"},
    "scale_half_at_5x":   {"f": 0.5, "k": 5.0, "trail": 0.10,
                           "what": "sell half at 5x the range, rest trails 10% behind a no-loss stop"},
}


def _bars(symbol: str, t0: float, t1: float) -> tuple[list, int]:
    """The finest bars available for this window."""
    for g in (60, 900, 3600):
        rows = db.query(
            "SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=? "
            "AND ts >= ? AND ts <= ? AND close IS NOT NULL ORDER BY ts",
            (symbol, g, t0, t1))
        if len(rows) >= 5:
            return [dict(r) for r in rows], g
    return [], 0


def _side_pct(symbol: str) -> float:
    try:
        from app.execution import rh_spread
        return float(rh_spread.get(symbol)["spread_pct"]) / 100.0
    except Exception:
        return SIDE_FALLBACK


def replay(trade: dict) -> list[dict]:
    """Every rule, on one trade's own bars, from the entry it actually got."""
    sym = trade["symbol"]
    t0 = float(trade["ts_open"])
    t1 = t0 + (MAX_HOLD_H + GRACE_H) * 3600.0
    bars, gran = _bars(sym, t0, t1)
    if not bars:
        return []

    side = _side_pct(sym)
    entry = float(trade["entry_px"])            # already includes the buy spread
    covered = entry / (1.0 - side)              # a sale here returns the money paid

    # The coin's own recent range, for the volatility-scaled rule.
    span = [b["high"] - b["low"] for b in bars[:24] if b["high"] and b["low"]]
    atr_frac = (sum(span) / len(span) / entry) if span and entry else 0.0

    # Is the coin climbing, at each bar? Computed ONCE for the whole replay and
    # only from bars at or before that bar -- a trend test that can see the
    # future is not a trend test, it is a way to make any rule look brilliant.
    windows = sorted({int(sp["trend_n"]) for sp in RULES.values() if sp.get("trend_n")})
    trend_by_n = {n: {} for n in windows}
    closes, lows = [], []
    for b in bars:
        closes.append(b["close"] or 0.0)
        lows.append(b["low"] or 0.0)
        for n in windows:
            ok = False
            if len(closes) >= 2 * n:
                avg = sum(closes[-n:]) / n
                floor_now = min(lows[-n:])
                floor_before = min(lows[-2 * n:-n])
                # Above its own recent average AND making higher lows. Both, so
                # one green bar in a downtrend does not count as a trend.
                ok = closes[-1] > avg and floor_now > floor_before
            trend_by_n[n][b["ts"]] = ok

    # The operator's day boundary is Austin's, not UTC's. A real timezone
    # conversion (it was a fixed -5h, which is wrong for half the year).
    hour_at = {b["ts"]: _dt.datetime.fromtimestamp(b["ts"], TZ).hour for b in bars}

    # THE DAY'S SHAPE, for the bear_cut rules. Needs bars from the day's local
    # midnight, which is before the entry, so a second query at the same
    # granularity. Bear at bar i = below the day's open AND lower highs AND
    # lower lows over the last n bars versus the n before. Only bars at or
    # before i are used; the flags are computed once.
    bear_windows = sorted({int(sp["bear_h"]) for sp in RULES.values() if sp.get("bear_h")})
    bear_by_h = {h: {} for h in bear_windows}
    if bear_windows and gran:
        day0 = _dt.datetime.fromtimestamp(t0, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        day_bars = db.query(
            "SELECT ts, high, low, close FROM bars WHERE symbol=? AND granularity=? "
            "AND ts >= ? AND ts < ? AND close IS NOT NULL ORDER BY ts",
            (sym, gran, day0.timestamp(), t0))
        day_bars = [dict(r) for r in day_bars]
        highs = [b["high"] or 0.0 for b in day_bars]
        lows = [b["low"] or 0.0 for b in day_bars]
        closes = [b["close"] or 0.0 for b in day_bars]
        day_open = closes[0] if closes else None
        day_of_entry = day0.date()
        for b in bars:
            # a bar past local midnight belongs to the next day: restart the shape
            d = _dt.datetime.fromtimestamp(b["ts"], TZ)
            if d.date() != day_of_entry:
                day_of_entry = d.date()
                highs, lows, closes = [], [], []
                day_open = None
            highs.append(b["high"] or 0.0); lows.append(b["low"] or 0.0); closes.append(b["close"] or 0.0)
            if day_open is None:
                day_open = closes[-1]
            for h in bear_windows:
                n = max(1, int(h * 3600 / gran))
                ok = False
                if len(closes) >= 2 * n and day_open:
                    ok = (closes[-1] < day_open
                          and max(highs[-n:]) < max(highs[-2 * n:-n])
                          and min(lows[-n:]) < min(lows[-2 * n:-n]))
                bear_by_h[h][b["ts"]] = ok

    out = []
    for name, spec in RULES.items():
        peak = entry
        peak_ts = t0
        result = None
        rule_state = {}          # fresh per rule PER TRADE, never shared
        for b in bars:
            age_h = (b["ts"] - t0) / 3600.0
            if b["high"] and b["high"] > peak:
                peak, peak_ts = b["high"], b["ts"]
            stale_h = (b["ts"] - peak_ts) / 3600.0
            _n = spec.get("trend_n")
            _bh = spec.get("bear_h")
            stop, tgt = spec["fn"](entry, peak, covered, age_h, atr_frac, stale_h, close=b["close"],
                                   trending=(trend_by_n[_n][b["ts"]] if _n else False),
                                   state=rule_state,
                                   hour_austin=hour_at[b["ts"]],
                                   bear=(bear_by_h[_bh].get(b["ts"], False) if _bh else False))
            if tgt is not None and b["high"] and b["high"] >= tgt:
                result = (tgt * (1 - side) / entry - 1, "target", age_h)
                break
            if b["low"] and b["low"] <= stop:
                px = min(stop, b["high"] or stop)     # cannot fill above the bar
                result = (px * (1 - side) / entry - 1, "stop", age_h)
                break
        if result is None:
            last = bars[-1]
            result = (last["close"] * (1 - side) / entry - 1, "time",
                      (last["ts"] - t0) / 3600.0)
        out.append({"trade_id": trade["id"], "rule": name,
                    "net_pct": result[0] * 100.0, "exit_reason": result[1],
                    "hours_held": result[2], "granularity": gran})

    for name, spec in SCALE_RULES.items():
        r = _scale_out(entry, covered, side, bars, t0,
                       spec["f"], spec["k"], spec["trail"], atr_frac)
        if r is None:
            continue
        out.append({"trade_id": trade["id"], "rule": name,
                    "net_pct": r[0] * 100.0, "exit_reason": r[1],
                    "hours_held": r[2], "granularity": gran})

    # ── THE CEILING AND THE FLOOR. Not rules. Not tradeable. ──────────────────
    #
    # 2026-09-22 the operator, looking at the "best it has been" column on the
    # Risk tab: "is there any way we can aim for the best it has been column?
    # ... or it's not possible because these are the numbers after the events,
    # so there's no way to predict them beforehand?"
    #
    # He answered it himself and he is right: a peak is only identifiable once
    # the price has come down from it, so no rule can exit there. What IS worth
    # knowing is how much is in the gap -- if perfect hindsight is only worth
    # half a percent more than holding, then exit tuning is not where the money
    # is and the search should move on.
    #
    # `_ceiling_peak` sells at the best price the coin ever showed while the
    # trade was open; `_floor_trough` at the worst. Both charge the same round
    # trip as every rule, so they sit on the same scoreboard. The leading
    # underscore keeps them sorted away from the rules and marks them as what
    # they are: the bounds of the game, not moves in it.
    highs = [b["high"] for b in bars if b["high"]]
    lows = [b["low"] for b in bars if b["low"]]
    if highs and lows and entry:
        best = max(highs)
        worst = min(lows)
        hi_bar = next(b for b in bars if b["high"] == best)
        lo_bar = next(b for b in bars if b["low"] == worst)
        out.append({"trade_id": trade["id"], "rule": BOUNDS[0],
                    "net_pct": (best * (1 - side) / entry - 1) * 100.0,
                    "exit_reason": "hindsight: the best price it ever showed",
                    "hours_held": (hi_bar["ts"] - t0) / 3600.0, "granularity": gran})
        out.append({"trade_id": trade["id"], "rule": BOUNDS[1],
                    "net_pct": (worst * (1 - side) / entry - 1) * 100.0,
                    "exit_reason": "hindsight: the worst price it ever showed",
                    "hours_held": (lo_bar["ts"] - t0) / 3600.0, "granularity": gran})
    return out


def absorb(limit: int = 500) -> dict:
    """Replay every closed trade that has not been replayed yet."""
    ensure_schema()
    # A trade is done only when EVERY current rule has a row for it. Keyed on
    # trade_id alone, a rule added later (the ride_* family, the flat_by_*
    # family) was replayed on the 8 trades that closed after it was written and
    # never on the 31 before -- so it sat at n=8 next to rules at n=39 and
    # could never reach a verdict. Adding a rule now back-fills it.
    # BOUNDS belong in `want` for the same reason every rule does: without them
    # the 113 trades already replayed count as "done" and the ceiling/floor
    # would only ever exist for trades closing from today on -- the exact shape
    # of the ride_*/flat_by_* bug this function was rewritten to kill.
    want = set(RULES) | set(SCALE_RULES) | set(BOUNDS)
    have: dict[int, set] = {}
    for r in db.query("SELECT trade_id, rule FROM exit_counterfactuals"):
        have.setdefault(int(r["trade_id"]), set()).add(r["rule"])
    done = {tid for tid, rules in have.items() if want <= rules}
    rows = db.query("SELECT id, symbol, strategy, entry_px, exit_px, ts_open, ts_close, "
                    "net_pnl_usd, qty FROM trades ORDER BY ts_close DESC LIMIT ?", (limit,))
    added = skipped = 0
    for t in rows:
        if t["id"] in done:
            continue
        try:
            results = replay(dict(t))
        except Exception as exc:
            db.log_event("WARNING", "exit_lab", f"trade {t['id']} replay failed: {exc}")
            continue
        if not results:
            skipped += 1
            continue
        for r in results:
            db.execute(
                "INSERT OR REPLACE INTO exit_counterfactuals(trade_id, rule, net_pct, "
                " exit_reason, hours_held, granularity, computed_at) VALUES (?,?,?,?,?,?,?)",
                (r["trade_id"], r["rule"], r["net_pct"], r["exit_reason"],
                 r["hours_held"], r["granularity"], time.time()))
        added += 1
    return {"replayed": added, "no_bars": skipped, "already_done": len(done)}


def standings(start: str | None = None, end: str | None = None,
              strategy: str | None = None) -> dict:
    """How each rule would have done, over a day or a range."""
    ensure_schema()
    where, args = ["1=1"], []
    if start:
        where.append("t.ts_close >= ?"); args.append(clock.bounds_for_day(start)[0])
    if end:
        where.append("t.ts_close < ?"); args.append(clock.bounds_for_day(end)[1])
    if strategy:
        where.append("t.strategy = ?"); args.append(strategy)
    rows = db.query(
        "SELECT c.rule AS rule, c.net_pct AS net_pct, c.exit_reason AS exit_reason, "
        "       c.hours_held AS hours_held, c.granularity AS granularity "
        "FROM exit_counterfactuals c JOIN trades t ON t.id = c.trade_id "
        f"WHERE {' AND '.join(where)}", tuple(args))

    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["rule"], []).append(dict(r))

    actual = db.query(
        "SELECT t.net_pnl_usd AS net, t.qty*t.entry_px AS basis FROM trades t "
        f"WHERE {' AND '.join(where)}", tuple(args))
    act = [100.0 * float(a["net"]) / float(a["basis"])
           for a in actual if a["basis"] and float(a["basis"]) > 0]

    out = []
    # The bounds are appended by replay() and live in neither rule table, so a
    # loop over RULES alone silently drops them -- 262 rows sat in the database
    # while the page showed 47 rules and no ceiling (2026-09-23).
    for name, spec in {**RULES, **SCALE_RULES,
                       **{b: {"what": BOUND_WHAT[b]} for b in BOUNDS}}.items():
        vals = [x["net_pct"] for x in by.get(name, []) if x["net_pct"] is not None]
        n = len(vals)
        if not n:
            continue
        mean = sum(vals) / n
        sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5 if n > 1 else 0.0
        se = sd / (n ** 0.5) if n else 0.0
        reasons: dict[str, int] = {}
        for x in by[name]:
            reasons[x["exit_reason"]] = reasons.get(x["exit_reason"], 0) + 1
        out.append({
            "rule": name, "what": spec["what"], "n": n,
            "mean_net_pct": mean, "se_pct": se,
            "total_net_pct": sum(vals),
            "win_rate": sum(1 for v in vals if v > 0) / n * 100.0,
            "best_pct": max(vals), "worst_pct": min(vals),
            "median_hours": sorted(x["hours_held"] for x in by[name])[n // 2],
            "exit_mix": reasons,
            "is_live_rule": name == "trail_8_breakeven",
            # a bound is a measuring stick, not a move the desk could make
            "tradeable": name not in BOUNDS,
        })
    # THE ROW THAT IS ACTUALLY LIVE: what the desk booked on these same trades,
    # after the same costs. Every strategy has its own exit (volume_build: fixed
    # target + catastrophe stop + 36h; day_climb: volatility-scaled target + 8%
    # stop + 24h; pump_ride: 8% trail + floor), so no single lab rule IS the
    # live rule -- the desk's own result is. On 2026-09-19 that number was
    # +1.12%/trade while the row labelled "running today" said -0.96%, and every
    # verdict on the page was measured against the wrong baseline.
    if act:
        n = len(act)
        mean = sum(act) / n
        sd = (sum((v - mean) ** 2 for v in act) / (n - 1)) ** 0.5 if n > 1 else 0.0
        out.append({
            "rule": "as_traded", "n": n,
            "what": "what the desk ACTUALLY booked with each strategy's own exit "
                    "(the baseline every rule is judged against)",
            "mean_net_pct": mean, "se_pct": sd / (n ** 0.5) if n else 0.0,
            "total_net_pct": sum(act),
            "win_rate": sum(1 for v in act if v > 0) / n * 100.0,
            "best_pct": max(act), "worst_pct": min(act),
            "median_hours": None, "exit_mix": {}, "is_live_rule": True,
            "tradeable": True,
        })
        for r in out:
            if r["rule"] != "as_traded":
                r["is_live_rule"] = False
    out.sort(key=lambda r: -r["mean_net_pct"])

    live = next((r for r in out if r["is_live_rule"]), None)
    for r in out:
        if live and live["n"]:
            diff = r["mean_net_pct"] - live["mean_net_pct"]
            sed = (r["se_pct"] ** 2 + live["se_pct"] ** 2) ** 0.5
            r["vs_live_pct"] = diff
            r["vs_live_sigmas"] = (diff / sed) if sed else 0.0
            r["verdict"] = ("not tradeable — hindsight only"
                            if not r.get("tradeable", True) else
                            "not enough trades yet" if r["n"] < MIN_FOR_A_VERDICT else
                            "BETTER" if r["vs_live_sigmas"] >= 2 else
                            "worse" if r["vs_live_sigmas"] <= -2 else
                            "not distinguishable")
    # THE NUMBER THAT MOTIVATES ALL OF THIS.
    #
    # Across the first 27 closed trades the average position showed +5.04% at its
    # best and was exited at -0.04%. Five points given back, per trade, against a
    # round trip of 1.92%. The exits — not the entries — are where the money is.
    give = db.query(
        "SELECT t.id AS id, t.symbol AS symbol, t.entry_px AS entry_px, "
        "       t.exit_px AS exit_px, t.ts_open AS ts_open FROM trades t "
        f"WHERE {' AND '.join(where)}", tuple(args))
    peaks, gots = [], []
    for t in give:
        try:
            bars, _ = _bars(t["symbol"], float(t["ts_open"]),
                            float(t["ts_open"]) + MAX_HOLD_H * 3600.0)
            if not bars:
                continue
            e = float(t["entry_px"]); side = _side_pct(t["symbol"])
            hi = max(b["high"] for b in bars if b["high"])
            peaks.append((hi * (1 - side) / e - 1) * 100.0)
            gots.append((float(t["exit_px"]) * (1 - side) / e - 1) * 100.0)
        except Exception:
            continue
    giveback = None
    if peaks:
        giveback = {
            "best_shown_pct": sum(peaks) / len(peaks),
            "actually_got_pct": sum(gots) / len(gots),
            "given_back_pts": (sum(peaks) - sum(gots)) / len(peaks),
            "n": len(peaks),
            "why": ("What the average trade showed at its best versus what it was "
                    "exited at. You cannot sell at the peak — you only know it was "
                    "the peak afterwards — but this is the size of the prize any "
                    "better exit is competing for."),
        }

    return {
        "rules": out,
        "giveback": giveback,
        "n_trades": max((r["n"] for r in out), default=0),
        "actual_mean_pct": (sum(act) / len(act)) if act else None,
        "min_for_a_verdict": MIN_FOR_A_VERDICT,
        "note": ("Same entries, same costs, both spreads charged. Stops are checked "
                 "against the bar's low and targets against its high, which never "
                 "flatters a challenger against the rule running today."),
    }
