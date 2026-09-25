"""Hard pre-trade risk checks. Nothing reaches a broker without passing all of them.

Every guard returns a reason string when it blocks, and every block is written
to the audit log. The kill switch is a FILE, not a flag in memory, so it works
when the web UI is dead, and it survives a process restart.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import get_settings
from app.core import clock, db
from app.core import mode as mode_mod
from app.strategy.time_budget import budget as _budget


@dataclass
class RiskDecision:
    allowed: bool
    reasons: list[str]
    checks: list[dict]

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": self.reasons, "checks": self.checks}


def _today_start() -> float:
    """The operator's local midnight, not UTC midnight.

    This returned UTC midnight = 19:00 Chicago, so the daily loss cap and the
    trade counter both reset at 7pm. A bot that had spent its entire daily loss
    allowance by 6pm was handed a fresh one an hour later, inside the same
    trading day, and the kill switch -- which reads this same counter -- never
    tripped.
    """
    return clock.local_day_start()


def realised_pnl_today(mode: str) -> float:
    row = db.query_one(
        "SELECT COALESCE(SUM(net_pnl_usd),0) p FROM trades WHERE mode=? AND ts_close>=?",
        (mode, _today_start()),
    )
    return float(row["p"]) if row else 0.0


def trades_today(mode: str) -> int:
    """NEW POSITIONS opened today -- not orders.

    This counted every order, so a single round trip burned two of the twelve
    allowed and closing a position could be refused because closing positions had
    used up the budget. The cap is about how many bets we place, not how many
    times we touch the broker. Exits are never rationed.
    """
    row = db.query_one(
        "SELECT COUNT(*) c FROM orders WHERE mode=? AND ts_decided>=? "
        "AND intent='open' AND status IN ('filled','submitted')",
        (mode, _today_start()),
    )
    return int(row["c"]) if row else 0


def prior_entry_today(mode: str, symbol: str) -> dict | None:
    """Have we already been in this coin today, and how strong was that signal?

    His observation, and his own book proved it inside two minutes on 2026-09-08:
    DOT closed at 12:12:45 for +$1.11, the bot climbed straight back in at
    12:14:31, and that second trade lost $10.30. A coin has roughly one move in
    it per day. Once we have taken it, going back for seconds is where the money
    goes.
    """
    row = db.query_one(
        "SELECT o.ts_decided, o.notional_usd, s.raw_score "
        "FROM orders o LEFT JOIN signals s ON s.id = o.signal_id "
        "WHERE o.mode=? AND o.symbol=? AND o.intent='open' "
        "AND o.status IN ('filled','submitted') AND o.ts_decided>=? "
        "ORDER BY o.ts_decided DESC LIMIT 1",
        (mode, symbol.upper(), _today_start()))
    if not row:
        return None
    return {"ts": float(row["ts_decided"]),
            "raw_score": float(row["raw_score"]) if row["raw_score"] is not None else None,
            "notional": float(row["notional_usd"] or 0.0)}


def account(mode: str) -> dict:
    """What the account is actually worth right now, and what is free to spend.

    `mode.get_equity()` returns the DECLARED stake -- the number typed into
    Setup. It never moves. Sizing off it means that after losing $100 the bot
    still sizes as though it had the full $500, and that a second position can be
    opened with money the first one is already using. This reads the live figure
    the engine writes every tick.
    """
    row = db.query_one(
        "SELECT equity, cash, positions_value, realised_pnl, unrealised_pnl "
        "FROM equity_curve WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,))
    declared = mode_mod.get_equity()
    stake_set_ts = None
    try:
        r2 = db.query_one("SELECT updated_ts FROM app_state WHERE key='account_equity'")
        stake_set_ts = float(r2["updated_ts"]) if r2 and r2["updated_ts"] else None
    except Exception:
        pass
    if not row:
        return {"equity": declared, "cash": declared, "deployed": 0.0,
                "realised_pnl": 0.0, "unrealised_pnl": 0.0, "declared_stake": declared,
                "stake_set_ts": stake_set_ts,
                "source": "declared stake -- no equity curve yet"}
    deployed = float(row["positions_value"] or 0.0)
    equity = float(row["equity"] or declared)
    return {"equity": equity,
            "cash": max(0.0, equity - deployed),
            "deployed": deployed,
            "realised_pnl": float(row["realised_pnl"] or 0.0),
            "unrealised_pnl": float(row["unrealised_pnl"] or 0.0),
            "declared_stake": declared,
            "stake_set_ts": stake_set_ts,
            "source": "live equity curve"}


def open_positions(mode: str) -> list[dict]:
    """Every open position, with the live truth about its stop.

    The Risk tab showed `stop_px` and nothing else, which hides the only two
    questions worth asking about a stop: is it FOLLOWING the price, and how far
    below the price is it right now. On 2026-09-19 the answer for all ten open
    positions was "no, and it has never moved" -- `trail_bps` was 0 on every one,
    so the ratchet in the engine was never entered -- and nothing in the app said
    so. A number that cannot move should not look like one that can.
    """
    rows = db.query("SELECT * FROM positions WHERE mode=? AND qty != 0", (mode,))
    for r in rows:
        px = None
        try:
            q = db.query_one(
                # The Robinhood row is written last in the same pass, so on a
                # tie it wins: the book is marked at the price it would exit at.
                "SELECT mid FROM quotes WHERE symbol=? ORDER BY ts DESC, id DESC LIMIT 1",
                (r["symbol"],))
            px = q["mid"] if q else None
        except Exception:
            px = None
        trail = float(r.get("trail_bps") or 0.0)
        stop = r.get("stop_px")
        entry = r.get("avg_px") or 0.0
        peak = r.get("peak_px") or entry
        r["price"] = px
        r["stop_kind"] = "trailing" if trail > 0 else "fixed"
        r["trail_pct"] = (trail / 100.0) if trail > 0 else None
        # How much room is left before the stop fires, as a percentage of the
        # CURRENT price -- the only version of this number you can act on.
        r["stop_room_pct"] = (((px - stop) / px) * 100.0
                              if px and stop else None)
        r["stop_vs_entry_pct"] = (((stop / entry) - 1.0) * 100.0
                                  if stop and entry else None)
        r["stop_moves"] = int(r.get("stop_moves") or 0)
        r["peak_gain_pct"] = (((peak / entry) - 1.0) * 100.0 if entry else None)
        # Above water means the stop is at or beyond the entry: the trade can no
        # longer lose money on the way out. Exactly zero positions have ever
        # reached this, which is worth being able to see.
        r["stop_above_entry"] = bool(stop and entry and stop >= entry)
        # What this position is worth RIGHT NOW, two ways. `unrealised_usd` is
        # mid-to-mid, what the coin has done. `net_if_closed_usd` is what a sell
        # at this instant would actually book, with Robinhood's sell-side spread
        # taken off -- the number the operator asked for on the Risk tab, and
        # the one that decides whether "up 1%" is a profit or still a loss.
        qty = float(r.get("qty") or 0.0)
        if px and entry and qty:
            try:
                from app.execution import rh_spread
                side = float(rh_spread.get(r["symbol"])["spread_pct"]) / 100.0
            except Exception:
                side = 0.0095
            r["unrealised_usd"] = qty * (px - entry)
            r["unrealised_pct"] = (px / entry - 1.0) * 100.0
            r["net_if_closed_usd"] = qty * (px * (1.0 - side) - entry)
            r["net_if_closed_pct"] = (px * (1.0 - side) / entry - 1.0) * 100.0
        else:
            r["unrealised_usd"] = r["unrealised_pct"] = None
            r["net_if_closed_usd"] = r["net_if_closed_pct"] = None
    return rows


def current_drawdown_pct(mode: str) -> float:
    # ORDER BY ts ASC LIMIT 20000 took the OLDEST 20,000 rows, not the recent
    # ones. The engine writes one row per tick, so within a few days the window
    # froze on ancient history: the drawdown stopped rising, and both the
    # max-drawdown check and the automatic kill switch quietly stopped working.
    rows = db.query(
        "SELECT equity FROM (SELECT ts, equity FROM equity_curve WHERE mode=? "
        "ORDER BY ts DESC LIMIT 20000) ORDER BY ts ASC", (mode,)
    )
    if len(rows) < 2:
        return 0.0
    peak, worst = -1e18, 0.0
    for r in rows:
        e = float(r["equity"])
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return worst


def _kill_switch_stale() -> str | None:
    """Was the kill switch tripped on an earlier trading day?

    It is a DAILY loss cap. Tripping it should stop trading for that day, not
    forever. It fired at 17:15 on 2026-09-08 because a strategy that has since
    been retired lost $16 of a $15 allowance, and it was still blocking every
    order the following morning -- 41 signals on DOT and 41 on NEAR rejected with
    "kill switch is engaged", which reads like the bot refusing to trade when it
    was actually just stuck on yesterday.

    In paper there is no money to protect, so a new day clears it and says so. In
    live it stays until a human releases it: a real loss deserves a human look.
    """
    s = get_settings()
    if not s.kill_switch_file.exists():
        return None
    try:
        first = s.kill_switch_file.read_text().splitlines()[0].strip()
        tripped = float(first)
    except Exception:
        return None
    if tripped >= _today_start():
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(tripped))


def clear_stale_kill_switch(mode: str) -> bool:
    """Release a kill switch left over from a previous day. Paper only."""
    if mode == "mcp":
        return False
    when = _kill_switch_stale()
    if not when:
        return False
    try:
        get_settings().kill_switch_file.unlink()
    except Exception:
        return False
    db.log_event("INFO", "risk",
                 f"kill switch from {when} released automatically — it was a DAILY "
                 f"loss cap and that day is over")
    return True


def engage_kill_switch(reason: str) -> None:
    s = get_settings()
    s.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
    s.kill_switch_file.write_text(f"{time.time()}\n{reason}\n")
    db.log_event("CRITICAL", "risk", f"KILL SWITCH ENGAGED: {reason}")


_OVERRIDE_KEY = "daily_loss_cap_waived_for_day"


def _today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(_today_start()))


def daily_loss_cap_waived_today() -> bool:
    """Did the operator release the switch today while the day's loss was
    already past the cap? Then the cap is waived for the rest of that day."""
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (_OVERRIDE_KEY,))
    return bool(row and row["value"] == _today_key())


def release_kill_switch() -> None:
    """Remove the switch -- and if the day's loss is still past the cap, record
    that the operator has seen it and chosen to go on: the cap is waived for
    the rest of THIS day, and the switch will not re-arm for that reason.

    2026-09-23: the desk was $71 down against a $60 cap. The operator released
    the switch nine times between 14:22 and 18:04 and it re-engaged within
    minutes each time, because the next signal re-read the same loss. A guard
    that re-arms two minutes after the person responsible turned it off is not
    a safeguard, it is a nag that teaches the operator to click through -- and
    the release event never said why it would not stick. Now the release IS
    the decision, logged as such, and it expires at midnight. The drawdown
    guard, the per-order checks and the book ceiling all still apply.
    """
    s = get_settings()
    loss = realised_pnl_today(mode_mod.get_mode())
    limit_usd = daily_loss_limit_usd()
    was_on = s.kill_switch_file.exists()
    if was_on:
        s.kill_switch_file.unlink()
    if loss <= -limit_usd:
        db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                   (_OVERRIDE_KEY, _today_key(), time.time()))
        db.log_event("WARNING", "risk",
                     f"kill switch released by operator with today's loss ${loss:.2f} past the "
                     f"${limit_usd:.2f} cap: the daily loss cap is WAIVED for the rest of "
                     f"{_today_key()}. Drawdown and per-order guards still apply.")
    elif was_on:
        db.log_event("WARNING", "risk", "kill switch released by operator")




def daily_loss_limit_usd() -> float:
    """The day's stop, as a share of equity rather than a dollar figure.

    A dollar cap goes stale the moment the account changes size: $15 was 3% of a
    $500 book and would have been 0.75% of a $2,000 one, which is a halt on an
    ordinary morning. So the percentage governs, and TC_MAX_DAILY_LOSS_USD is an
    optional absolute override -- 0 or unset means "just use the percentage".
    """
    from app.core import mode as mode_mod
    s = get_settings()
    pct = daily_loss_pct()
    pct_limit = mode_mod.get_equity() * pct / 100.0
    hard = float(s.max_daily_loss_usd or 0.0)
    return min(pct_limit, hard) if hard > 0 else pct_limit


_PCT_KEY = "daily_loss_pct_override"

# A cap of zero is not caution, it is a desk that halts on the first cent. A cap
# of 90% is not a cap. Both ends are refused rather than silently clamped.
PCT_MIN, PCT_MAX = 0.5, 50.0


def daily_loss_pct() -> float:
    """The day's stop as a percent of stake, changeable while running.

    It used to live only in `.env`, which meant changing it was: find the file,
    edit it, restart the app. The operator's standing instruction is that
    everything should be doable in the app, and a risk limit you have to stop
    the desk to adjust is one people work around instead of adjusting.

    The override lives in app_state and wins over `.env` when present. Clearing
    it falls back to the file, so the .env value is still the documented default
    rather than being overwritten by a click.
    """
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (_PCT_KEY,))
    if row and row["value"]:
        try:
            v = float(row["value"])
            if PCT_MIN <= v <= PCT_MAX:
                return v
        except (TypeError, ValueError):
            pass
    return float(get_settings().max_daily_loss_pct)


def set_daily_loss_pct(pct: float | None) -> dict:
    """Set the day's stop, or pass None to go back to the .env default."""
    from app.core import mode as mode_mod
    if pct is None:
        db.execute("DELETE FROM app_state WHERE key=?", (_PCT_KEY,))
        db.log_event("WARNING", "risk",
                     "daily loss cap reset to the .env default "
                     f"({get_settings().max_daily_loss_pct}%)")
        return {"pct": daily_loss_pct(), "source": "env"}
    v = float(pct)
    if not (PCT_MIN <= v <= PCT_MAX):
        raise ValueError(
            f"the day's stop must be between {PCT_MIN}% and {PCT_MAX}% of the "
            f"stake. {v}% is {'not a cap at all' if v > PCT_MAX else 'a halt on the first small loss'}."
        )
    before = daily_loss_limit_usd()
    db.execute("INSERT INTO app_state(key, value, updated_ts) VALUES (?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
               "updated_ts=excluded.updated_ts",
               (_PCT_KEY, str(v), time.time()))
    after = mode_mod.get_equity() * v / 100.0
    db.log_event("WARNING", "risk",
                 f"daily loss cap changed by the operator: {v}% of stake "
                 f"(${before:.2f} -> ${after:.2f})")
    return {"pct": v, "source": "operator", "was_usd": before, "now_usd": after}



def open_risk_usd(mode: str) -> float:
    """What the open book would lose if every stop fired right now.

    The one number that says how exposed the desk actually is, and it was not
    being computed anywhere. Positions were sized one at a time against free
    cash, so ten positions each individually reasonable could add up to a loss
    far larger than the day's budget — which is precisely what had happened when
    the operator found a $1,132 ARB position risking $90 against a $60 cap.

    A position with no stop is counted at its full notional: an exit that depends
    on a trailing stop that has not ratcheted yet is not a floor.
    """
    total = 0.0
    for p in db.query("SELECT qty, avg_px, stop_px FROM positions "
                      "WHERE mode=? AND qty > 0", (mode,)):
        qty = abs(float(p["qty"] or 0.0))
        entry = float(p["avg_px"] or 0.0)
        stop = float(p["stop_px"] or 0.0)
        if qty <= 0 or entry <= 0:
            continue
        total += qty * (entry - stop) if 0 < stop < entry else qty * entry
    return total


def min_notional_for(symbol: str) -> float:
    """Smallest order the venue will accept for this coin, in dollars.

    This is the only floor in the sizing rule, and it is what ends a day's
    trading: each position is a share of what is left, so the remainder shrinks,
    and when the next share lands under this number there is no more trading.
    An emergent stop rather than a chosen one.

    ONE SOURCE, and a placeholder. Robinhood publishes `min_order_size` per
    trading pair; `sync_universe` converts it to dollars into
    `universe.min_notional`. Until that has run, the floor is $1 and is LABELLED
    a placeholder.

    It used to fall back to "the smallest notional this desk has ever filled",
    which sounded empirical and was circular: it read $60.61 purely because no
    position had happened to be smaller, and then refused every position under
    $60.61 on that basis. Within hours it had blocked 157 VVV entries and 156 ZEC
    entries on a book with $283 of free cash. A floor has to come from the venue,
    not from our own history of not having gone lower.
    """
    from app.core import db
    row = db.query_one("SELECT min_notional FROM universe WHERE symbol=?", (symbol,))
    if row and row["min_notional"] is not None and float(row["min_notional"]) > 0:
        return float(row["min_notional"])
    return 1.0


def floor_source(symbol: str) -> str:
    """Where this coin's minimum came from, so a placeholder is never mistaken
    for a measurement."""
    from app.core import db
    row = db.query_one("SELECT min_notional FROM universe WHERE symbol=?", (symbol,))
    if row and row["min_notional"]:
        return "robinhood_published"
    seen = db.query_one("SELECT MIN(notional_usd) AS m FROM orders "
                        "WHERE status='filled' AND notional_usd > 0")
    if seen and seen["m"]:
        return "smallest_fill_observed"
    return "placeholder_until_universe_sync"


# The lowest odds a target may have and still be taken. Deliberately low: see
# the note at the check itself. 0.35 sits just under the worst shape the desk
# has actually produced (37%), so it refuses only shapes worse than anything in
# the record -- a 5x+ ratio, which lands 26% of the time or less.
MIN_P_REACH = 0.35

# Refuse a bar in which this strategy found exactly one candidate. See the note
# at the check. Set False to take them again.
BLOCK_LONE_CANDIDATE = True


def _hourly_range_frac(symbol: str) -> float:
    """The coin's average hourly range over the last 24h. Same input the engine
    and the trade plan use, so the gate and the plan cannot disagree."""
    try:
        rows = db.query(
            "SELECT high, low, close FROM bars WHERE symbol=? AND granularity=3600 "
            "AND close IS NOT NULL AND close > 0 ORDER BY ts DESC LIMIT 24", (symbol,))
        rng = [(float(r["high"]) - float(r["low"])) / float(r["close"])
               for r in rows if r["high"] and r["low"] and r["close"]]
        return sum(rng) / len(rng) if rng else 0.0
    except Exception:
        return 0.0


def pre_trade_check(
    *,
    symbol: str,
    side: str,
    notional_usd: float,
    mode: str,
    intent: str = "open",
    feed_agreement: dict | None = None,
    rh_confirmed: bool | None = None,
    raw_score: float | None = None,
    stop_frac: float | None = None,
    strategy: str | None = None,
    target_frac: float | None = None,
    field_size: int | None = None,
) -> RiskDecision:
    s = get_settings()
    clear_stale_kill_switch(mode)
    checks: list[dict] = []
    reasons: list[str] = []

    def chk(name: str, ok: bool, detail: str, limit=None, actual=None):
        checks.append({"check": name, "passed": bool(ok), "detail": detail,
                       "limit": limit, "actual": actual})
        if not ok:
            reasons.append(detail)

    chk("kill_switch", not s.kill_switch_file.exists(),
        "kill switch is engaged -- no orders may be placed", None, None)

    # THE OPERATOR'S OWN SWITCH. Per-strategy: off, a daily budget, a per-trade
    # ceiling -- all settable in the app, no restart, no engineer. It sits here
    # rather than inside the strategy so that turning a rule off cannot be
    # bypassed by any caller that builds an order some other way.
    #
    # It refuses ENTRIES ONLY. A strategy that is switched off keeps managing
    # the positions it already holds all the way to their own exits; stranding
    # money in a position with nothing watching it would be a far worse failure
    # than the one this switch exists to prevent.
    from app.risk import control as _control
    for _reason in _control.check(strategy, notional_usd, mode, intent):
        chk("strategy_control", False, _reason, None, None)

    if intent == "open":
        pos = open_positions(mode)
        # A count of open positions is NOT a reason to refuse a trade. It was,
        # and on 2026-09-16 that cost the book the two best entries of the day:
        # day_climb fired ZEC at 01:00 and NEAR at 06:00, both were refused for
        # "already holding 2 positions", and the desk bought them twelve hours
        # later near the high. The limiter is cash. If the money is there and the
        # signal is worth it, the trade happens, whether it is the first of the
        # day or the eleventh.
        #
        # Set TC_MAX_CONCURRENT_POSITIONS above 0 only to pin the book for a
        # deliberate experiment; 0 (the default) means the cap does not exist.
        if s.max_concurrent_positions > 0:
            chk("max_concurrent_positions", len(pos) < s.max_concurrent_positions,
                f"already holding {len(pos)} positions (operator pinned the book at "
                f"{s.max_concurrent_positions}; 0 disables this)",
                s.max_concurrent_positions, len(pos))

        # Runaway backstop. If this ever trips, sizing is broken -- say so rather
        # than presenting it as a policy the desk meant to have.
        n = trades_today(mode)
        chk("max_trades_per_day", n < s.max_trades_per_day,
            f"{n} orders today hit the runaway backstop of {s.max_trades_per_day}. "
            f"This is not a trading limit, it is a loop detector -- cash should "
            f"have stopped this long ago, so the sizing is wrong",
            s.max_trades_per_day, n)

        chk("max_position_usd", notional_usd <= s.max_position_usd,
            f"order notional ${notional_usd:.2f} exceeds ${s.max_position_usd:.2f} "
            f"({s.max_position_frac:.0%} of equity) -- a bug ceiling, not a size",
            s.max_position_usd, notional_usd)

        # THE FLOOR, which is the real thing that ends a day's trading. A position
        # has to clear the venue's own minimum order size for that coin -- read
        # from Robinhood's trading-pairs endpoint, not invented here. As cash gets
        # committed each new position is a share of a smaller remainder, so the
        # book stops opening positions when the next one would be too small to
        # place. That is what replaces a slot count.
        floor = min_notional_for(symbol)
        chk("min_notional", notional_usd >= floor,
            f"${notional_usd:.2f} is below {symbol}'s ${floor:.2f} minimum order "
            f"size -- the cash left will not fund another position",
            floor, notional_usd)

        # The "stronger signal" exception below let a coin be bought twice in one
        # day. That is pyramiding, which is not what this book is for, and the
        # positions table cannot represent two entries in the same coin anyway.
        # One position per coin, full stop; re-entry waits for the exit.
        # One position per coin PER STRATEGY. The book keeps a row per
        # (symbol, strategy) and closes each on its own rule, so a pump rule
        # may open its own, separately managed pair in a coin a climb rule is
        # already riding -- the operator's "treat it as a separate trade". The
        # venue pools the holding; our ledger does not. What bounds the stacked
        # exposure is the book-risk ceiling above, not a per-coin count. The
        # same strategy still may not add to its own position (no averaging).
        same_strat = [pp for pp in pos if pp["symbol"] == symbol
                      and (strategy is None or pp["strategy"] == strategy)]
        stacked_on = [pp["strategy"] for pp in pos if pp["symbol"] == symbol and pp not in same_strat]
        chk("no_double_entry", not same_strat,
            f"{symbol} is already held by {strategy or 'this strategy'} -- one position per "
            f"coin per strategy, and re-entry waits until it closes", None, None)

        # COIN STACKING -- two strategies in the same coin at once. This was
        # deliberately ALLOWED ("treat it as a separate trade"), on the reasoning
        # that a pump rule and a climb rule are different ideas. Paused
        # 2026-09-22 at the operator's request, and his argument is structural
        # rather than statistical:
        #
        #   "what I was trying to do with this approach was, later on, maybe if
        #    there's a very clear high probability opportunity, then we take it.
        #    Not at the same time, entering the same stock with two different
        #    strategies. That doesn't make sense."
        #
        # What the open book looked like when he said it: CHIP held by
        # volume_build and pump_catch entered in the SAME MINUTE, SHIB five
        # minutes apart, $476 of a ~$2,000 book doubled up across four coins.
        # Two rules firing on one coin inside five minutes are not two ideas;
        # they are one idea bought twice, paying the round trip twice for it.
        #
        # THE EVIDENCE DOES NOT YET AGREE, and that is stated rather than hidden:
        # only three stacked trades have closed, and they made +$5.36 (+$1.79 a
        # trade against +$0.65 for the rest). Three trades decide nothing. So
        # this is paused as an EXPERIMENT, not retired as a proven loser -- every
        # refusal is recorded with its full features under the reason below, so
        # `research/coin_stacking.py` can replay what the blocked entries would
        # have returned and answer the question properly.
        # THE LONE CANDIDATE. 2026-09-23, measured on the 72 closed trades that
        # can be matched back to their race: a bar in which only ONE coin in the
        # whole universe qualified returned **-3.11%/trade** against **+0.57%**
        # when two or more did -- 14 trades vs 58, win rate 21% vs 66%,
        # permutation p = 0.007. On day_climb alone it was -5.18% vs +0.56%.
        #
        # The mechanism is plausible and that matters at n=14: one coin
        # triggering on its own is an idiosyncratic move, while several
        # triggering together is the market actually doing something. A lone
        # trigger is the strategy finding noise.
        #
        # Small sample, real p-value, sound mechanism, and the blocked trades
        # cost -$25.29. Blocked, logged, one constant to turn it off.
        if BLOCK_LONE_CANDIDATE and field_size is not None:
            chk("lone_candidate", int(field_size) != 1,
                f"{symbol} was the ONLY coin in the universe that qualified for "
                f"{strategy or 'this strategy'} this bar. Bars with a single "
                f"candidate have returned -3.11% a trade against +0.57% when two "
                f"or more qualified (14 vs 58 trades, p=0.007) -- one coin moving "
                f"alone is usually noise, not a market", 1, int(field_size))

        # THE SHAPE GATE. 2026-09-23, the operator: "only promoted when the
        # chances are high to profit."
        #
        # A target is not "7%" in the abstract -- it is some number of the coin's
        # own typical hourly moves, and that ratio decides how often it is ever
        # reached: 75% at 1.5x, 27% at 5x, 7% at 14x (283k windows, holdout
        # validated -- strategy/time_budget.py).
        #
        # MEASURED BEFORE SHIPPING, and the measurement says be careful: on the
        # 69 closed trades with a recoverable target, a gate at 30% would have
        # refused NOTHING, at 40% two trades worth -$0.67, and at 50% fourteen
        # trades worth +$7.97 -- it would have destroyed the book. So this is a
        # RAIL against a shape the desk has not produced yet, not an optimiser.
        # Set low on purpose. Raising it past ~0.40 is contradicted by evidence.
        if target_frac and target_frac > 0:
            _atr = _hourly_range_frac(symbol)
            _b = _budget(target_frac, _atr)
            chk("shape_odds", _b["p_reach"] >= MIN_P_REACH,
                f"{symbol}: a +{target_frac*100:.2f}% target is {_b['ratio']:.1f}x this "
                f"coin's typical hourly move, and targets that size are reached only "
                f"{_b['p_reach']*100:.0f}% of the time (needs {MIN_P_REACH*100:.0f}%). "
                f"The shape is the problem, not the coin",
                MIN_P_REACH, _b["p_reach"])

        chk("coin_stacking", not stacked_on,
            f"{symbol} is already held by {', '.join(stacked_on)}. A second strategy "
            f"entering the same coin is paused (coin_stacking, in the lab since "
            f"2026-09-22) -- it is one idea bought twice and it pays the spread "
            f"twice. This entry is recorded so the lab can price what it would "
            f"have made", None, len(stacked_on))

        # A COIN USED TO GET ONE MOVE A DAY, and getting back in required a
        # "stronger signal than the first". That rule was written down, not
        # measured: nothing in this repo has ever shown that a second entry in a
        # coin does worse than the first. It is the same mistake as the slot cap
        # and the hour window — a plausible-sounding restriction that quietly
        # decides which trades never happen.
        #
        # It is now recorded instead of enforced. `prior_entries_today` rides
        # along on the signal's features, so when there is enough live history
        # the retrainer can find out whether re-entry actually costs anything,
        # and a rule can be fitted rather than assumed. Until then, the only
        # per-coin restriction is the structural one above: one position at a
        # time, because the positions table holds one row per coin.
        prior = prior_entry_today(mode, symbol)
        checks.append({"check": "prior_entry_today", "passed": True,
                       "detail": (f"{symbol} was already traded today"
                                  if prior is not None else
                                  f"first {symbol} entry today"),
                       "limit": None,
                       "actual": (prior["raw_score"] if prior else None)})

        acct = account(mode)
        chk("cash_available", notional_usd <= acct["cash"] + 1e-9,
            f"order needs ${notional_usd:.2f} but only ${acct['cash']:.2f} is uncommitted "
            f"(${acct['deployed']:.2f} is already in open positions)",
            acct["cash"], notional_usd)

        # THE BOOK-LEVEL CEILING. Cash was the only limiter, and on 2026-09-20
        # at 12:00 nine day_climb signals fired in one bar and were all funded:
        # 19 positions, $1,244 deployed, and if every stop had fired the book
        # would have lost $108.87. The rule: the book may never be positioned to
        # lose more than the desk can absorb and still trade tomorrow.
        #
        #     open risk (every stop firing) + this order's risk  <=  headroom
        #
        # The budget is the DRAWDOWN budget (max_drawdown_pct of equity, the
        # same figure the kill switch and the `book_risk_within_drawdown`
        # invariant use), less whatever has already been lost today. It was the
        # daily loss cap for one day (2026-09-20 -> 21): at 3% of a $2,000 book
        # that is $60, eleven positions with 8% stops already sit at $60, and it
        # refused 32 entries on the morning of the 21st -- "I don't want any
        # limitation, I want to raise the bar". The daily cap stays what it
        # was, a realised-loss stop; this ceiling is the catastrophe bound, and
        # at 10% of equity it did not bind on any day so far. Signals arrive in
        # the strategy's own rank order, so if it ever binds the best-ranked
        # get funded and the rest are refused with this reason. A position with
        # no stop is counted at its full notional, as open_risk_usd does.
        acct_equity = float(acct.get("equity") or mode_mod.get_equity())
        headroom = (acct_equity * float(s.max_drawdown_pct) / 100.0
                    + min(0.0, realised_pnl_today(mode)))
        open_risk = open_risk_usd(mode)
        new_risk = notional_usd * float(stop_frac) if stop_frac and stop_frac > 0 else notional_usd
        chk("book_risk_ceiling", open_risk + new_risk <= headroom + 1e-9,
            f"the book already risks ${open_risk:.2f} if every stop fires; this order adds "
            f"${new_risk:.2f} against a ${headroom:.2f} drawdown budget "
            f"({s.max_drawdown_pct:.0f}% of equity, less today's realised loss). "
            f"Lower-ranked signals in the same bar are refused here so the book is never "
            f"positioned to lose more than the desk can survive",
            headroom, open_risk + new_risk)

    loss = realised_pnl_today(mode)
    limit_usd = daily_loss_limit_usd()
    waived = loss <= -limit_usd and daily_loss_cap_waived_today()
    chk("daily_loss_cap", loss > -limit_usd or waived,
        f"today's realised P&L ${loss:.2f} breaches the ${limit_usd:.2f} daily loss cap",
        -limit_usd, loss)
    if waived:
        checks[-1]["note"] = (f"today's loss ${loss:.2f} is past the ${limit_usd:.2f} cap; the "
                              f"operator released the switch and waived the cap for {_today_key()}")

    dd = current_drawdown_pct(mode)
    chk("max_drawdown", dd <= s.max_drawdown_pct,
        f"drawdown {dd:.1f}% exceeds the {s.max_drawdown_pct:.1f}% ceiling", s.max_drawdown_pct, dd)

    if feed_agreement is not None:
        chk("feed_cross_check", bool(feed_agreement.get("agree")),
            f"price feeds disagree: {feed_agreement.get('reason')}",
            feed_agreement.get("tolerance_bps"), feed_agreement.get("diff_bps"))

    if mode == "mcp":
        chk("live_confirmation", s.live_enabled,
            "TC_LIVE_CONFIRM is not set to the exact confirmation string; live trading is disabled")
        if rh_confirmed is not None:
            chk("symbol_confirmed_on_robinhood", bool(rh_confirmed),
                f"{symbol} has not been confirmed tradeable by the Robinhood MCP")

    decision = RiskDecision(allowed=not reasons, reasons=reasons, checks=checks)
    if not decision.allowed:
        db.log_event("WARNING", "risk", f"order blocked for {symbol} {side}",
                     {"reasons": reasons, "notional": notional_usd, "mode": mode})
    if loss <= -limit_usd and not waived and not s.kill_switch_file.exists():
        engage_kill_switch(f"daily loss cap breached: ${loss:.2f} vs ${limit_usd:.2f}")
    if dd > s.max_drawdown_pct and not s.kill_switch_file.exists():
        engage_kill_switch(f"max drawdown breached: {dd:.1f}%")
    return decision


def status(mode: str = "paper") -> dict:
    s = get_settings()
    loss = realised_pnl_today(mode)
    limit_usd = daily_loss_limit_usd()
    return {
        "mode": mode,
        "kill_switch_engaged": s.kill_switch_file.exists(),
        "kill_switch_reason": (s.kill_switch_file.read_text().strip().splitlines()[-1]
                               if s.kill_switch_file.exists() else None),
        "realised_pnl_today_usd": loss,
        "daily_loss_limit_usd": limit_usd,
        "daily_loss_pct": daily_loss_pct(),
        "daily_loss_pct_source": (
            "operator" if db.query_one(
                "SELECT 1 FROM app_state WHERE key=?", (_PCT_KEY,)) else "env"),
        "daily_loss_pct_bounds": [PCT_MIN, PCT_MAX],
        "stake_usd": mode_mod.get_equity(),
        "daily_loss_headroom_usd": limit_usd + loss,
        "daily_loss_cap_waived_today": daily_loss_cap_waived_today(),
        # The book-level ceiling, as the pre-trade check sees it: the drawdown
        # budget, not the daily cap. Both are shown so the operator can see
        # when the book is positioned to lose more than a day's cap in one go.
        "open_risk_usd": open_risk_usd(mode),
        "book_risk_headroom_usd": (float(account(mode).get("equity") or mode_mod.get_equity())
                                   * float(s.max_drawdown_pct) / 100.0 + min(0.0, loss)),
        "book_risk_exceeds_daily_cap": bool(open_risk_usd(mode) > limit_usd + min(0.0, loss)),
        "orders_today": trades_today(mode),
        "max_trades_per_day": s.max_trades_per_day,
        "open_positions": open_positions(mode),
        "max_concurrent_positions": s.max_concurrent_positions,
        "drawdown_pct": current_drawdown_pct(mode),
        "max_drawdown_pct": s.max_drawdown_pct,
        "live_enabled": s.live_enabled,
    }
