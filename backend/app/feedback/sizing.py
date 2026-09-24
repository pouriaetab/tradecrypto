"""How big a position is, and therefore how many positions exist.

WHY THIS FILE EXISTS
--------------------
The operator has said it three times now, and each time the number came back in
a different costume:

    "again why do you keep making hard caps hard fixed codes, i said randomly 8
     we could do 2 trades or none or 1 or 10 or more within that budget. please
     stop setting hard rule around numeric examples i give"

He was right, and the specific failure is worth naming so it does not recur. He
said "that would let us take 8 trades or more" as an illustration of what $2,000
buys. That got written into the config as max_concurrent_positions=8, then
probe_fraction=0.125 (which is 1/8), then max_position_usd=400 (which is 8 slots
with headroom). One offhand number became three settings that could only produce
eight trades, and the system stopped being able to discover that a given day
wanted two, or eleven, or none.

So there is no slot count in this system. The number of positions open at any
moment is an OUTPUT, and it comes from two things that are measured rather than
chosen:

    1. How often this strategy speaks. A strategy that fires nine times a day
       cannot put 45% of the book on each one; a strategy that fires once a
       fortnight should not put 5% on its one idea and leave the account idle.
       So the baseline share of free cash is 1 / (signals expected today).
       That is measured from the strategy's own signal history.

    2. Whether a louder signal is actually a better one. This is an empirical
       question with an empirical answer, and for most strategies the answer is
       no. If conviction does not predict returns, the curve is FLAT -- every
       qualifying signal gets the same share -- and the code says so out loud
       instead of ramping the size on a hunch.

Everything else falls out. Positions are funded from free cash, so a big first
position leaves no room for a second, and a day of marginal signals fragments
the book into many small ones until the next one would land under the venue's
minimum order size. Ten trades and zero trades are both reachable states of the
same rule, which is the whole point.

THE ONE THING THAT IS NOT MEASURED
----------------------------------
SHARE_FLOOR and SHARE_CEIL are arithmetic, not opinion: you cannot spend more
than the cash you have, and a share small enough to land under the venue floor
is the same as not trading. They bound the output of the formula; they do not
decide anything inside it.
"""
from __future__ import annotations

import json
import math
import time

from app.config import get_settings
from app.core import clock, db

KEY = "sizing_curve"

# Arithmetic bounds on a fraction of free cash, not a view about position size.
SHARE_FLOOR = 0.05
SHARE_CEIL = 0.95
# The most of free cash one position may take. Replaced `1 / signals per day` on
# 2026-09-22 (see base_share). A quarter means the book tapers to roughly eight
# to ten positions before a share drops under the venue's minimum order size,
# without any slot count existing.
SHARE_CAP = 0.25
# The most trades one day's loss budget is ever divided among. Beyond this the
# per-trade risk is so small the position cannot carry its own round trip.
MAX_FILLS_FOR_BUDGET = 10.0

# A slope needs this much evidence before it moves the size at all. Below it the
# shrinkage term does the work smoothly; this is only the point where the fitted
# curve is reported as "flat" rather than "weak".
MIN_SAMPLES = 40
# |t| at which a fitted slope earns half its raw weight. t=2 is the usual "two
# standard errors" line, and squaring makes the shrink gentle near it rather
# than a cliff.
T_HALF = 2.0


# ─────────────────────────────── observation ────────────────────────────────

def signals_per_day(strategy: str, days: float = 30.0) -> float:
    """How often this strategy actually speaks, from its own history.

    Counts distinct signal bars rather than rows, because a strategy that emits
    two candidates in one bar still only gets one funding decision.
    """
    since = time.time() - days * 86400.0
    row = db.query_one(
        "SELECT COUNT(DISTINCT CAST(ts/3600 AS INTEGER)) AS n, "
        "       MIN(ts) AS first_ts "
        "FROM signals WHERE strategy=? AND side='buy' AND ts>=?",
        (strategy, since))
    if not row or not row["n"]:
        return 0.0
    span_days = max(1.0, (time.time() - float(row["first_ts"])) / 86400.0)
    return float(row["n"]) / span_days


def desk_signals_per_day() -> float:
    """How often the WHOLE desk speaks, not one strategy.

    The share used to be 1 / (this strategy's signals per day), computed by each
    strategy independently — so five strategies each concluded they could have
    most of the book, which cannot all be true at once. Worse, any strategy
    firing once a day or less got 1/1 = 100%, clipped to 95%.

    That is what put $1,132 of a $2,000 account into a single ARB position on
    2026-09-18: pump_ride fires about once a day, so it asked for 95% of free
    cash and got it. The cash is shared, so the denominator has to be shared.
    """
    from app.strategy.registry import ACTIVE_STRATEGIES
    return sum(signals_per_day(s) for s in ACTIVE_STRATEGIES)


def base_share(strategy: str) -> float:
    """The most of FREE CASH one position may take. A concentration cap, nothing else.

    WHAT THIS USED TO BE, AND WHY IT WAS WRONG -- 2026-09-22
    -------------------------------------------------------
    It was `1 / desk_signals_per_day()`: the day's cash spread over the day's
    expected ideas. That sounds principled and is not, because the denominator
    counts how TALKATIVE the desk is, which has nothing to do with how good an
    opportunity is or how much money the book can hold.

    What it did, measured on this book:

        date     desk signals/day   share   avg position
        Sep 14         10.7          9.4%      $172
        Sep 18         16.2          6.2%       $91
        Sep 21         25.6          3.9%*      $64     (*clipped up to the 5% floor)

    The universe grew from 33 coins to 75. The same unchanged rule -- day_climb,
    version 0.1, params c3ba79714a throughout -- then found setups in 29 coins
    across 22 hours instead of 6 coins across 9, so the denominator tripled and
    EVERY strategy's position halved. Nobody decided that. By Sep 21 every
    day_climb position was between $69 and $70: the standard deviation of
    position size within a day fell from $60 to $0.34.

    That is an averaging machine. Many identical small bets, each paying the same
    fixed round trip, pull the day's result toward the mean of a large sample --
    and the mean, net of a 1.9% toll, sits at zero. The operator described the
    symptom before anyone found the cause: "we continuously stay within some
    profit range or loss up and down".

    WHAT IT IS NOW
    --------------
    A flat ceiling on concentration: no single position may take more than
    SHARE_CAP of what is uncommitted. It does not move with activity, and that
    is the entire point.

    It still tapers by itself, because it is a share of FREE cash and free cash
    falls as positions open. At 25% and $1,000 free:

        $250, then $187, then $140, then $105, ...

    The book gets deep enough to diversify and stops before it is made of dust,
    the earliest (best-ranked) signals of a bar get the most money, and no slot
    count was reintroduced -- the operator removed those deliberately and cash
    remains the only limiter.

    Sizing is decided by what a trade can LOSE (equal_risk_notional) and bounded
    by this. Never by how often the desk speaks.
    """
    return SHARE_CAP


def fills_per_day(days: float = 30.0) -> float:
    """How many entries the desk fills on a TYPICAL day it trades.

    The median, not the mean. The mean over a trailing window is dragged by one
    busy stretch and then stays dragged: on 2026-09-20 this desk filled 26 times
    in a day, which pulled the average to 12 and cut every later trade's risk
    budget in half -- the same runaway the cash share had (see base_share). The
    median of the days it actually traded is what a normal day looks like and
    does not move because of one outlier.

    Counts fills, not signals: signals are cheap and mostly rejected; fills are
    what consume the loss budget, so fills are what the budget is divided among.
    """
    # Bucketed in Python, not with SQLite's `localtime`: that is the timezone of
    # whatever machine runs the query, so the same SQL gives Chicago days on the
    # operator's Mac and UTC days in CI -- and UTC midnight is 19:00 Chicago, so
    # every evening's fills would land on the next day. See core/clock.day_key.
    rows = db.query(
        "SELECT ts_filled AS t FROM orders WHERE side='buy' AND status='filled' "
        "AND ts_filled >= ?", (time.time() - days * 86400.0,))
    per_day: dict[str, int] = {}
    for r in rows:
        if r["t"] is None:
            continue
        k = clock.day_key(float(r["t"]))
        per_day[k] = per_day.get(k, 0) + 1
    counts = sorted(n for n in per_day.values() if n > 0)
    if not counts:
        return 1.0
    mid = len(counts) // 2
    med = counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) / 2.0
    return float(max(1.0, med))


def risk_budget_per_trade(mode: str = "paper") -> tuple[float, str]:
    """How many dollars this trade is allowed to lose.

    The day's loss cap, divided by the number of entries the desk typically
    fills in a day. No new parameter: both halves already exist, and by
    construction a normal day's trades add up to the day's budget rather than
    each being sized in ignorance of the others.
    """
    from app.risk import guards
    cap = guards.daily_loss_limit_usd()
    rate = fills_per_day()
    budget = cap / max(1.0, rate)
    # A FLOOR, because this is the second divisor that was tracking the desk's
    # own activity. More fills -> smaller budget -> smaller positions -> cash
    # left over -> room for more fills. That loop has no bottom, and a trade
    # risking pennies is not a smaller version of a trade, it is noise that
    # still pays a full round trip. The floor is the day's cap over
    # MAX_FILLS_FOR_BUDGET, so however busy a week gets, one trade is never
    # sized as though the day held more than that many of them.
    floor = cap / MAX_FILLS_FOR_BUDGET
    if budget < floor:
        return floor, (f"${cap:,.0f} daily cap / {rate:.0f} fills a day would be "
                       f"${budget:.2f}; floored at ${floor:.2f} (never sized as though "
                       f"a day held more than {MAX_FILLS_FOR_BUDGET:.0f} trades)")
    return budget, f"${cap:,.0f} daily cap / {rate:.1f} fills a day (median)"


def equal_risk_notional(stop_frac: float, mode: str = "paper") -> tuple[float, str]:
    """Position size from the stop distance, so every trade risks the same.

    THE PROBLEM THIS FIXES. Sizing was a share of free cash, which ignores where
    the exit is. A coin stopped at 5% and one stopped at 15% got the SAME dollars
    and therefore three times different risk — the wild coin quietly carrying
    triple the exposure of the calm one, with nothing on any screen saying so.

    Equal risk inverts it: pick what a trade may lose, divide by how far the stop
    is, and the size falls out.

        risk budget $9.47   (a $60 day / 6.3 fills a day)

            5% stop  ->  $189   a calm coin, tight exit
            8% stop  ->  $118   ARB, day_climb
           10% stop  ->   $95   pump_ride's catastrophe stop
           15% stop  ->   $63   volume_build on a wild coin
           25% stop  ->   $38   very wild

    Every one of those loses $9.47 if it stops out. That is the point: the coin's
    volatility decides the size, not the other way round.
    """
    if stop_frac <= 0:
        return 0.0, "no stop distance — cannot size by risk"
    budget, why = risk_budget_per_trade(mode)
    return budget / stop_frac, f"risks ${budget:.2f} at a {stop_frac:.1%} stop ({why})"


def risk_capped_notional(want: float, stop_frac: float, mode: str = "paper") -> tuple[float, str]:
    """Shrink a position until what it can lose is something the desk can afford.

    THIS IS THE GUARD THAT WAS MISSING, and the ARB trade showed exactly why: a
    $1,132 position with an 8% trailing stop risks $90.57, against a daily loss
    cap of $60. One position could lose one and a half times the entire day's
    budget — and nothing anywhere said no.

    Two limits, both derived from settings that already exist rather than picked:

      1. A SINGLE position may not risk more than the whole daily loss cap. If
         one trade can end the trading day by itself, it is too big.
      2. The WHOLE BOOK's risk — every open position measured to its own stop,
         plus this one — may not exceed the drawdown limit that halts the desk.
         If every stop fired at once, that must not be the thing that stops the
         desk trading.

    It SHRINKS rather than refuses. A trade that is too big is a sizing question,
    not a reason to skip the idea.
    """
    from app.risk import guards
    if stop_frac <= 0:
        return want, "no stop distance known — risk not capped"

    try:
        cap = guards.daily_loss_limit_usd()
        equity = guards.account(mode)["equity"]
        dd_budget = equity * float(get_settings().max_drawdown_pct) / 100.0
        open_risk = guards.open_risk_usd(mode)
    except Exception:
        return want, "risk figures unavailable — not capped"

    per_trade = cap / stop_frac
    room = max(0.0, dd_budget - open_risk)
    portfolio = room / stop_frac
    allowed = min(want, per_trade, portfolio)

    if allowed >= want - 0.01:
        return want, ""
    if per_trade <= portfolio:
        why = (f"trimmed ${want:,.0f} -> ${allowed:,.0f}: at a {stop_frac:.1%} stop "
               f"the larger size risks more than the ${cap:,.0f} daily loss cap")
    else:
        why = (f"trimmed ${want:,.0f} -> ${allowed:,.0f}: the book already risks "
               f"${open_risk:,.0f} to its stops against a ${dd_budget:,.0f} drawdown limit")
    return allowed, why


def _clip(x: float) -> float:
    return float(min(SHARE_CEIL, max(SHARE_FLOOR, x)))


# ──────────────────────────────── the fit ───────────────────────────────────

def fit(strategy: str, samples: list[tuple[float, float]]) -> dict:
    """Does a louder signal pay better? Regress outcome on log(conviction).

    `samples` is [(conviction_x, realised_return_pct)]. conviction_x is the
    signal's score as a multiple of its own entry threshold, which is the only
    scale that is comparable across strategies.

    Log, because conviction is a ratio: the step from 1x to 2x is the same kind
    of step as 2x to 4x, and an untransformed fit lets one 12x outlier write the
    curve by itself.

    The slope is then shrunk toward zero by t^2/(t^2 + T_HALF^2). A slope with
    t=0.5 keeps 6% of its size; a slope with t=4 keeps 80%. This is the same
    discipline the hour profile uses, and it exists because this project has
    twice shipped a rule fitted to noise and had to back it out.
    """
    pts = [(float(c), float(r)) for c, r in samples
           if c is not None and r is not None
           and math.isfinite(float(c)) and math.isfinite(float(r)) and float(c) > 0]
    n = len(pts)
    out = {"strategy": strategy, "n": n, "slope": 0.0, "raw_slope": 0.0,
           "t": 0.0, "shrink": 0.0, "flat": True, "fitted_ts": time.time(),
           "why": ""}
    if n < MIN_SAMPLES:
        out["why"] = (f"{n} closed samples, need {MIN_SAMPLES} before a slope means "
                      f"anything -- every signal is sized the same until then")
        return out

    xs = [math.log(c) for c, _ in pts]
    ys = [r for _, r in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        out["why"] = "every signal had identical conviction -- nothing to fit"
        return out
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    resid = [y - (my + b * (x - mx)) for x, y in zip(xs, ys)]
    if n <= 2:
        return out
    s2 = sum(r * r for r in resid) / (n - 2)
    se = math.sqrt(s2 / sxx) if s2 > 0 and sxx > 0 else 0.0
    t = (b / se) if se > 0 else 0.0
    shrink = (t * t) / (t * t + T_HALF * T_HALF) if t else 0.0

    out.update(raw_slope=b, t=t, shrink=shrink, slope=b * shrink,
               flat=abs(b * shrink) < 1e-9)
    if out["flat"]:
        out["why"] = (f"conviction does not predict outcome here (slope {b:+.3f}%/e-fold, "
                      f"t={t:+.2f}, kept {shrink:.0%}) -- so every qualifying signal "
                      f"gets the same share")
    else:
        out["why"] = (f"louder signals paid {b:+.3f}%/e-fold, t={t:+.2f}, kept "
                      f"{shrink:.0%} after shrinkage")
    return out


def share(strategy: str, conviction: float | None, fallback: float) -> tuple[float, str]:
    """Fraction of FREE CASH this signal gets, and the sentence explaining it."""
    base = base_share(strategy)
    rate = signals_per_day(strategy)
    if rate > 0:
        why = f"{rate:.1f} signals/day expected -> {base:.0%} of free cash each"
    else:
        why = f"no signal history yet -> {base:.0%} of free cash"

    if conviction is None:
        return _clip(fallback if fallback > 0 else base), why + " (no conviction on this signal)"

    cur = curve(strategy)
    slope = float(cur.get("slope") or 0.0)
    if abs(slope) < 1e-9:
        return base, why + "; conviction curve is flat (measured, not assumed)"

    # A slope in "% return per e-fold of conviction" is turned into a size
    # multiplier by its ratio to the strategy's own typical move, so a strategy
    # whose trades swing 10% is not resized by a 0.2% slope.
    scale = float(cur.get("typical_move_pct") or 0.0)
    c = max(1.0, float(conviction))
    if scale > 0:
        mult = 1.0 + (slope * math.log(c)) / scale
    else:
        mult = 1.0
    return _clip(base * mult), (
        why + f"; x{mult:.2f} for conviction {c:.1f}x ({cur.get('why', '')})")


# ─────────────────────────────── persistence ────────────────────────────────

def save(strategy: str, profile: dict) -> None:
    db.execute(
        "INSERT INTO app_state(key, value, updated_ts) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (f"{KEY}:{strategy}", json.dumps(profile), time.time()))


def curve(strategy: str) -> dict:
    row = db.query_one("SELECT value FROM app_state WHERE key=?", (f"{KEY}:{strategy}",))
    if not row:
        return {"slope": 0.0, "flat": True, "n": 0,
                "why": "not fitted yet -- flat until there is something to fit"}
    try:
        return json.loads(row["value"])
    except Exception:
        return {"slope": 0.0, "flat": True, "n": 0, "why": "unreadable curve, treated as flat"}


def refit_all(mode: str = "paper") -> dict:
    """Refit every strategy's conviction curve from CLEAN closed trades.

    Defect-affected trades are excluded, for the same reason they are excluded
    from every other learner here: they measure a bug, not a market.
    """
    from app.feedback import defects
    rows = db.query(
        "SELECT t.strategy AS strategy, t.ts_close AS ts_close, t.ts_open AS ts_open, "
        "       t.entry_px AS entry_px, t.net_pnl_usd AS net_pnl_usd, "
        "       t.qty AS qty, s.features_json AS features "
        "FROM trades t "
        "LEFT JOIN orders o ON o.id = t.open_order_id "
        "LEFT JOIN signals s ON s.id = o.signal_id "
        "WHERE t.mode=?", (mode,))
    by_strategy: dict[str, list[tuple[float, float]]] = {}
    moves: dict[str, list[float]] = {}
    skipped_unlinked = 0
    for r in rows:
        if defects.is_excluded(dict(r)):
            continue
        if not r["features"]:
            skipped_unlinked += 1
            continue
        try:
            feats = json.loads(r["features"])
        except Exception:
            continue
        c = feats.get("conviction_x")
        basis = float(r["entry_px"] or 0) * float(r["qty"] or 0)
        if c is None or basis <= 0 or r["net_pnl_usd"] is None:
            continue
        net_pct = float(r["net_pnl_usd"]) / basis * 100.0
        by_strategy.setdefault(r["strategy"], []).append((float(c), net_pct))
        moves.setdefault(r["strategy"], []).append(abs(net_pct))

    out = {"skipped_unlinked": skipped_unlinked, "fitted": {}}
    for strat, samples in by_strategy.items():
        prof = fit(strat, samples)
        mv = sorted(moves.get(strat, []))
        prof["typical_move_pct"] = (mv[len(mv) // 2] if mv else 0.0)
        prof["signals_per_day"] = signals_per_day(strat)
        prof["base_share"] = base_share(strat)
        save(strat, prof)
        out["fitted"][strat] = prof
    return out
