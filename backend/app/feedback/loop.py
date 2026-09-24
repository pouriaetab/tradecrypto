"""The reward / penalty loop.

What "learning" means here, concretely: after every closed trade the system
decomposes what happened, updates a Bayesian belief about that strategy's true
net edge, and reallocates capital accordingly. No hidden state, no opaque model
weights -- just a posterior you can read on screen and check by hand.

Attribution decomposition for a closed trade
--------------------------------------------
    predicted_edge   what the strategy said it would earn, in bps
    realised_gross   what the price actually did
    cost_paid        entry + exit execution cost, measured
    timing_slip      the part of the cost caused by delay rather than spread
    residual         realised_gross - predicted_edge  (model error)

A strategy can be "right" (residual ~ 0) and still lose money if cost_paid
exceeds predicted_edge. Distinguishing those two failure modes is the entire
point: the first says fix the model, the second says stop trading this venue.
"""
from __future__ import annotations

import json
import time

import numpy as np

from app.config import get_settings
from app.core import db
from app.core import mode as mode_mod
from app.execution import cost_model
from app.research.stats import BetaBinomial, NormalInverseGamma, kelly_with_uncertainty

ROLLING_TRADES = 300          # regime protection: only recent trades inform the belief


def attribute_trade(trade_id: int) -> dict:
    t = db.query_one("SELECT * FROM trades WHERE id=?", (trade_id,))
    if not t:
        return {}
    notional = abs(t["qty"] * t["entry_px"]) or 1.0
    gross_bps = t["gross_pnl_usd"] / notional * 1e4
    cost_bps = t["cost_usd"] / notional * 1e4
    # Every trade used to come back "model wrong", including a +$14.60 winner.
    #
    # The cause was a degenerate comparison, not a judgement. `predicted_edge_bps`
    # is written as NULL by the engine, because every active strategy sets its
    # expected edge to ZERO ON PURPOSE -- they are uncalibrated experiments and
    # calibrate() deliberately does nothing. So `predicted` was always 0.0, and
    # the test reduced to `abs(gross_bps) > 20`: any move bigger than 0.2% in
    # EITHER direction was filed as a model error. A strategy that predicts
    # nothing cannot be wrong, and calling a profitable trade "model wrong" told
    # the operator nothing except that the label was broken.
    #
    # What a single trade can honestly say is only: did the coin go our way, and
    # did the spread leave anything. Whether the strategy is any good is a
    # question about MANY trades -- which is exactly the operator's own point
    # ("out of so many trades we were wrong most of the time, but this one we got
    # lucky") -- so the running record is attached here rather than inferred from
    # one row. feedback/relearn.py is where that judgement actually lives.
    predicted = t["predicted_edge_bps"]
    net_usd = t["net_pnl_usd"] or 0.0
    has_prediction = predicted is not None and abs(float(predicted)) > 1e-9

    if gross_bps > 0 and net_usd > 0:
        verdict = "right, and it paid"
    elif gross_bps > 0:
        verdict = "right direction, the spread took it"
    elif net_usd > 0:
        verdict = "paid, but the coin went against us"
    else:
        verdict = "wrong direction"

    # Start from whatever is already stored. Recovery writes keys this function
    # does not own -- `provenance`, `replaced_fabricated_close` -- and replacing
    # the whole blob would erase the only record that a row was salvaged rather
    # than observed. Merge; never clobber.
    try:
        existing = json.loads(t["attribution_json"] or "{}")
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}

    attribution = {
        "predicted_edge_bps": float(predicted) if has_prediction else None,
        "prediction_made": has_prediction,
        "realised_gross_bps": gross_bps,
        "cost_paid_bps": cost_bps,
        "net_bps": net_usd / notional * 1e4,
        "verdict": verdict,
        "note": (None if has_prediction else
                 "this strategy predicts no edge by design, so one trade is evidence, "
                 "not error — see the strategy scorecard for the record over many"),
    }
    if has_prediction:
        attribution["model_error_bps"] = gross_bps - float(predicted)
        attribution["model_verdict"] = (
            "model wrong" if abs(gross_bps - float(predicted)) > max(abs(float(predicted)), 20)
            else "as predicted")

    # The running record for this strategy, so a lucky single win is visible as
    # one row inside a bigger sample rather than read as skill.
    try:
        rec = db.query_one(
            """SELECT COUNT(*) n, SUM(CASE WHEN net_pnl_usd > 0 THEN 1 ELSE 0 END) w,
                      SUM(net_pnl_usd) tot
               FROM trades WHERE strategy=? AND mode=?""", (t["strategy"], t["mode"]))
        if rec and rec["n"]:
            attribution["strategy_record"] = (
                f"{t['strategy']}: {rec['w'] or 0}/{rec['n']} profitable, "
                f"${rec['tot']:+.2f} total")
    except Exception:
        pass
    # A salvaged row's ORIGINAL verdict died with the database. This one is
    # recomputed from the numbers that survived, which is honest arithmetic --
    # but it is not the label the desk wrote at the time, and the difference has
    # to be visible or it is just a nicer-looking gap (blueprint 1.10).
    if existing.get("provenance") and not existing.get("verdict"):
        attribution["verdict_recomputed_at"] = time.time()
        attribution["verdict_note"] = (
            "recomputed on " + time.strftime("%Y-%m-%d", time.gmtime()) +
            " from the stored entry, exit, quantity and cost. The label written "
            "when this trade closed was lost with the database; the numbers it "
            "is derived from were recovered and corroborated.")

    merged = {**existing, **attribution}
    db.execute("UPDATE trades SET attribution_json=?, realised_edge_bps=? WHERE id=?",
               (json.dumps(merged), gross_bps, trade_id))
    return merged


def label_unlabelled_trades() -> dict:
    """Give a verdict to every closed trade that has none. Idempotent.

    Deliberately NOT a run-once repair. `reattribute_trades_once()` was one, and
    it had already marked itself done before the trades salvaged out of the
    corrupt database were inserted -- so four rows sat in the Journal with "—"
    where the verdict goes, and nothing in the system was ever going to fix
    them. Anything that can happen again needs a check that runs again
    (blueprint 5.3).
    """
    done, failed = 0, 0
    try:
        rows = db.query(
            """SELECT id FROM trades
               WHERE ts_close IS NOT NULL
                 AND (attribution_json IS NULL
                      OR json_extract(attribution_json, '$.verdict') IS NULL)""")
    except Exception:
        # json_extract is available in every SQLite we ship against, but fall
        # back rather than skip the repair if it ever is not.
        rows = [r for r in db.query("SELECT id, attribution_json FROM trades "
                                    "WHERE ts_close IS NOT NULL")
                if '"verdict"' not in (r["attribution_json"] or "")]
    for r in rows:
        try:
            attribute_trade(r["id"])
            done += 1
        except Exception:
            failed += 1
    if done:
        try:
            from app.core import liveness
            liveness.fired("verdict_backfilled", f"{done} trade(s)")
        except Exception:
            pass
    if done or failed:
        db.log_event("INFO" if not failed else "WARNING", "feedback",
                     f"labelled {done} trade(s) that had no verdict"
                     + (f"; {failed} could not be labelled" if failed else ""))
    return {"labelled": done, "failed": failed, "checked": len(rows)}


def _recent_net_returns(strategy: str, mode: str,
                        with_weights: bool = False):
    """Recent per-trade net returns, oldest first -- and, when asked, one
    weight per trade for how much the rule that made it resembles the rule
    running now (research/versions.py). A trade from a removed version weighs
    zero; from a lightly tuned version, nearly one."""
    rows = db.query(
        """SELECT net_pnl_usd, qty, entry_px, params_hash FROM trades
           WHERE strategy=? AND mode=? ORDER BY ts_close DESC LIMIT ?""",
        (strategy, mode, ROLLING_TRADES),
    )
    out, kept = [], []
    for r in rows:
        notional = abs(r["qty"] * r["entry_px"])
        if notional > 0:
            out.append(r["net_pnl_usd"] / notional)
            kept.append(dict(r))
    arr = np.array(out[::-1], dtype=float)
    if not with_weights:
        return arr
    try:
        from app.research import versions
        w = versions.weights_for(strategy, kept[::-1])
    except Exception:
        w = np.ones_like(arr)
    return arr, w


def update_posterior(strategy: str, mode: str = "paper") -> dict:
    """Recompute a strategy's belief from its recent trades, and reallocate."""
    s = get_settings()
    r, w = _recent_net_returns(strategy, mode, with_weights=True)
    hurdle = cost_model.hurdle_bps() / 1e4     # in return units

    nig = NormalInverseGamma().update(r, weights=w)
    wins = float(w[r > 0].sum()) if r.size else 0.0
    bb = BetaBinomial().update(wins, float(w.sum()) - wins)
    p_above = nig.prob_above(hurdle) if r.size else 0.0
    lo, _hi = nig.mean_ci(0.90) if r.size else (0.0, 0.0)

    # Allocation: size on the lower confidence bound, quarter-Kelly, and only if
    # we are confident the edge clears the cost hurdle.
    kelly = kelly_with_uncertainty(lo, nig.var_estimate) if r.size else 0.0
    allocation = float(np.clip(kelly * s.kelly_fraction, 0.0, 1.0)) if p_above >= 0.75 else 0.0

    n_eff = float(w.sum()) if r.size else 0.0
    if n_eff < s.min_trades_for_live:
        status = "paper"
        reason = (f"{n_eff:.0f}/{s.min_trades_for_live} trades before live is even considered"
                  + (f" ({r.size} closed; earlier rule versions discounted)" if r.size and n_eff < r.size - 0.5 else ""))
    elif allocation <= 0:
        status = "halted"
        reason = (f"P(edge > cost hurdle) = {p_above:.2f}; needs >= 0.75. "
                  f"Cost hurdle is {hurdle*1e4:.0f} bps per round trip.")
    else:
        status = "live"
        reason = f"P(edge > hurdle) = {p_above:.2f}, allocation {allocation:.3f} of equity"

    payload = {
        "return_posterior": nig.to_dict(),
        "hit_rate_posterior": bb.to_dict(),
        "prob_edge_above_hurdle": p_above,
        "cost_hurdle_bps": hurdle * 1e4,
        "kelly_raw": kelly,
        "kelly_fraction_applied": s.kelly_fraction,
        "n_trades_considered": int(r.size),
        "n_effective": float(w.sum()) if r.size else 0.0,
        "version_weighting": ("all trades count in full" if r.size and float(w.min()) >= 0.999
                              else f"{int((w < 0.999).sum())} trade(s) from earlier rule versions discounted"
                              if r.size else "no trades"),
        "rolling_window": ROLLING_TRADES,
    }
    db.execute(
        """INSERT INTO strategy_state(ts, strategy, status, allocation_frac, posterior_json, reason)
           VALUES (?,?,?,?,?,?)""",
        (time.time(), strategy, status, allocation, json.dumps(payload), reason),
    )
    return {"strategy": strategy, "status": status, "allocation_frac": allocation,
            "reason": reason, **payload}


def allocations(mode: str = "paper") -> list[dict]:
    """Latest state for every strategy that has ever traded."""
    strats = [r["strategy"] for r in db.query("SELECT DISTINCT strategy FROM trades WHERE mode=?", (mode,))]
    out = []
    for s in strats:
        row = db.query_one(
            "SELECT * FROM strategy_state WHERE strategy=? ORDER BY ts DESC LIMIT 1", (s,))
        if row:
            out.append({**row, "posterior": json.loads(row["posterior_json"])})
    return out


def position_size_usd(strategy: str, mode: str = "paper",
                      conviction: float | None = None,
                      stop_bps: float | None = None,
                      trail_bps: float | None = None,
                      size_mult: float = 1.0) -> float:
    """Notional for the next trade, from the posterior, capped by the risk config.

    The probe used to be a flat 5% of equity. On a $500 account that is a $25
    position -- and a $25 position pays $0.48 in spread to open and close, so it
    needs a 2% move just to break even and a 10% move to make $2.50. Sprinkling
    small positions is the worst possible shape for a small account facing a wide
    spread: the cost is proportional to notional, so halving the position halves
    the profit but does nothing to the percentage hurdle.

    Concentration is the correct response. Fewer, larger, more selective.
    `probe_fraction` is now the knob and it is sized against
    max_concurrent_positions so that a full book is roughly fully invested.
    """
    s = get_settings()
    row = db.query_one("SELECT * FROM strategy_state WHERE strategy=? ORDER BY ts DESC LIMIT 1", (strategy,))
    alloc = float(row["allocation_frac"]) if row else 0.0
    if alloc <= 0:
        # No demonstrated edge yet -> a deliberately small probe, PAPER ONLY.
        # `mode` was accepted and then ignored, so a strategy with no edge --
        # which is every strategy in this repo today -- still got 5% of equity
        # sized as a real order in live and advisory. "No edge claimed means no
        # trade placed" has to be enforced here, not just written down.
        if mode != "paper":
            return 0.0
        return _sized_for(strategy, s.probe_fraction, mode, conviction, stop_bps, trail_bps,
                          size_mult=size_mult)
    return _sized_for(strategy, alloc, mode, conviction, stop_bps, trail_bps, size_mult=size_mult)


def _sized_for(strategy: str, fraction: float, mode: str, conviction: float | None,
               stop_bps: float | None = None, trail_bps: float | None = None,
               size_mult: float = 1.0) -> float:
    return _sized(fraction, mode, conviction, strategy=strategy,
                  stop_bps=stop_bps, trail_bps=trail_bps, size_mult=size_mult)


def _sized(fraction: float, mode: str, conviction: float | None = None,
           strategy: str = "", stop_bps: float | None = None,
           trail_bps: float | None = None, size_mult: float = 1.0) -> float:
    """How much to put on this trade.

    There is no fixed position size and no slot count. The number comes out of
    three things the system measures, in this order:

    1. WHAT IS LEFT. Free cash, from the live equity curve. Never more than that,
       so a second position can never spend the first one's money. This is also
       what decides how MANY positions exist: when the cash is committed, nothing
       new opens until something closes, and when a share of the remainder would
       land under the venue's minimum order size the day is simply over. Two
       positions, or none, or eleven, are all ordinary outcomes.

    2. HOW OFTEN THIS STRATEGY SPEAKS. The baseline share of free cash is
       1 / (signals expected today), measured from the strategy's own history.
       day_climb fires ~9 times a day and sizes accordingly; pump_ride fires
       every few days and takes most of the book when it does. Nobody typed
       either number.

    3. WHETHER A LOUDER SIGNAL IS A BETTER ONE. Measured per strategy, shrunk
       toward zero, and flat by default -- see feedback/sizing.py. If conviction
       has never predicted outcome for this strategy, every qualifying signal
       gets the same share and the app says so.

    `fraction` survives only as the fallback for a strategy with no signal
    history at all, and `max_position_usd` is a bug ceiling derived from equity.
    """
    from app.risk import guards
    from app.feedback import sizing
    s = get_settings()
    acct = guards.account(mode)
    cash = acct["cash"]
    share, _why = sizing.share(strategy, conviction, float(fraction))
    by_cash = cash * share

    # EQUAL RISK IS THE PRIMARY RULE; the cash share is now a ceiling on it.
    #
    # Size comes from the stop distance, so every trade risks the same amount
    # whatever the coin's volatility. The share of free cash still applies as an
    # upper bound — it is what stops one idea monopolising the book — but it no
    # longer decides the number on its own, because it never knew where the exit
    # was.
    stop_frac = _stop_fraction(stop_bps, trail_bps)
    by_risk, _rwhy = sizing.equal_risk_notional(stop_frac, mode) if stop_frac > 0 else (by_cash, "")
    want = min(by_cash, by_risk) if by_risk > 0 else by_cash
    # THE WEATHER. research/regime_days.py hands the engine a multiplier for
    # this strategy in today's regime -- 1.0 unless the replay says this
    # strategy does measurably better or worse on days like today, and never
    # outside [0.5, 1.5]. Applied here, after the risk-equalised size and
    # before the caps, so a regime can tilt a size but never breach a cap.
    try:
        m = float(size_mult)
    except (TypeError, ValueError):
        m = 1.0
    if m > 0 and abs(m - 1.0) > 1e-9:
        want *= m
    want = float(max(0.0, min(s.max_position_usd, want, cash)))
    # And then trimmed to what it can afford to lose. See sizing.risk_capped_notional.
    capped, _note = sizing.risk_capped_notional(want, stop_frac, mode)
    return float(max(0.0, capped))


def _stop_fraction(stop_bps: float | None, trail_bps: float | None) -> float:
    """How far below entry the exit sits, as a fraction. The TIGHTER of the two
    is what actually gets hit first, and therefore what the risk is measured to."""
    vals = [float(v) / 1e4 for v in (stop_bps, trail_bps) if v]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else 0.0



def sizing_explanation(strategy: str, mode: str = "paper",
                       conviction: float | None = None) -> dict:
    """The same arithmetic, but showing its work -- for the app and for tests."""
    from app.risk import guards
    from app.feedback import sizing
    s = get_settings()
    acct = guards.account(mode)
    share, why = sizing.share(strategy, conviction, s.probe_fraction)
    want = acct["cash"] * share
    size = float(max(0.0, min(s.max_position_usd, want, acct["cash"])))
    return {"strategy": strategy, "cash": acct["cash"], "equity": acct["equity"],
            "share": share, "want": want, "size_usd": size,
            "ceiling_usd": s.max_position_usd, "conviction": conviction,
            "why": why,
            "left_after": max(0.0, acct["cash"] - size)}
