"""Which coins we track and which we trade — decided by criteria, not by hand.

The honest history of this file: the original 50-coin list was written from
memory. That is the weakest thing in the project, and the operator was right to
challenge it. It cannot notice that FIL is not tradeable on Robinhood, it cannot
drop a coin that has gone quiet, and it cannot pick up a new listing.

This replaces it with a rule.

Four roles
----------
    core        always tracked, whether or not we trade it. BTC, ETH and the
                other majors set the tone for everything else, so breadth, the
                leader-trend reading and the lead-lag basket all need them
                present even in a week when none of them is worth trading.
    tradeable   passes every gate; strategies may open positions here.
    watch       tracked for context, never traded — the FIL case: real coin,
                real data, not available on Robinhood.
    excluded    dropped from collection entirely.

The gates, in order
-------------------
    1. tradable on Robinhood        hard gate once a sync has happened
    2. liquidity                    median daily dollar volume above a floor
    3. cost                         estimated round-trip below a ceiling
    4. movement                     realised volatility inside a band
    5. data quality                 enough history, few enough gaps

Gate 4 is the one people get wrong in both directions. Too little volatility and
no strategy can clear the spread — a coin that moves 0.3% a day cannot pay for a
1.5% round trip however good the signal is. Too much and position sizing becomes
guesswork and stops get run by noise.

Hysteresis
----------
A coin is promoted when its score clears `enter_score` and demoted only after it
sits below `exit_score` (a LOWER bar) for `exit_patience` consecutive reviews.
Without that gap the borderline coins flip in and out every day, every flip pays
a spread, and the ledger fills with churn that looks like strategy.
"""
from __future__ import annotations

import json
import time

import numpy as np

from app.core import db
from app.execution import symbol_cost

# Majors that set the tone. Always collected; traded only if they also pass.
CORE = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

DEFAULTS = dict(
    # Was 25, and that cap -- not merit -- is what kept the desk small.
    #
    # Measured 2026-09-11: Robinhood confirms 58 coins. 23 were being traded.
    # The 35 held back were NOT failing the 0.55 entry bar -- they scored 0.63
    # to 0.72, comfortably above it. They were losing on the ranking cap alone,
    # with the recorded reason "did not make the top 25". PEPE, SHIB, SEI, COMP,
    # OP and HYPE were all in that group, and PEPE was one of eight coins that
    # passed the volume and climb gates on the morning of 09-11.
    #
    # The operator's whole thesis is that a handful of coins take turns moving
    # each day. Watching 23 of 58 means missing most of the rotation, and the
    # cap was doing that silently. The score bar, the liquidity floor and the
    # per-coin cost hurdle below are the real filters, and there is no
    # concurrent-position cap downstream any more either -- cash and the venue's
    # minimum order size decide how many positions exist. So nothing needs this
    # number to keep the book small.
    #
    # 0 DISABLES IT, which is the intended state: a coin that clears every real
    # filter is watched, however many that turns out to be.
    max_tradeable=0,
    # The liquidity gate is now derived from the ORDER WE ACTUALLY PLACE (see
    # `_order_size_usd` and `_impact_gate`). This dollar floor is kept only as
    # a labelled comparison in the gate's output, so the change is measurable
    # against what the old rule would have said. It no longer decides anything.
    min_dollar_volume=2_000_000.0,   # the OLD fixed floor -- comparison only
    max_round_trip_bps=200.0,        # 2% round trip is already brutal
    min_vol_bps=15.0,                # below this a day's range cannot pay the spread
    max_vol_bps=400.0,               # above this sizing and stops stop meaning much
    # 2,000 was arbitrary, and on 2026-09-11 it was the ONLY thing standing
    # between this desk and four of the best coins on the board:
    #
    #   ZEC    $120.0M/day  1.92% round trip  1,830 bars  <- blocked by 170 bars
    #   HYPE    $44.1M/day  1.92% round trip  1,838 bars  <- blocked by 162 bars
    #   AERO     $5.7M/day  1.92% round trip  1,784 bars
    #   BNB      $2.8M/day  1.92% round trip  1,769 bars
    #
    # All four clear the liquidity floor and the cost ceiling. All four have
    # 100%-complete history. They were excluded for being ~75 days old instead
    # of ~83, on a desk whose deepest strategy warm-up is 342 bars -- so 1,769
    # bars is already five times what it takes to run.
    #
    # The gate that guards against fitting on thin data is model_lab's own
    # `required_bars = warmup * 4`, which is separate and untouched. This one
    # only decides whether a coin is worth WATCHING, and three times the deepest
    # warm-up is an honest bar for that.
    min_bars=1000,
    min_completeness_pct=80.0,
    enter_score=0.55,
    exit_score=0.35,                 # deliberately lower than enter_score
    exit_patience=3,
)


def ensure_schema() -> None:
    for col, decl in (("role", "TEXT"), ("score", "REAL"),
                      ("last_reviewed", "REAL"), ("below_exit_count", "INTEGER"),
                      ("selection_json", "TEXT")):
        try:
            db.execute(f"ALTER TABLE universe ADD COLUMN {col} {decl}")
        except Exception:
            pass  # already there
    db.execute("""CREATE TABLE IF NOT EXISTS universe_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL, symbol TEXT NOT NULL,
        from_role TEXT, to_role TEXT, score REAL, reason TEXT)""")


def _metrics(days: float = 30.0) -> dict[str, dict]:
    """Liquidity, volatility and data quality per coin, from stored bars."""
    since = time.time() - days * 86400
    rows = db.query(
        """SELECT symbol, granularity, COUNT(*) n, MIN(ts) a, MAX(ts) b,
                  AVG(close) px, AVG(volume) v
           FROM bars WHERE ts >= ? GROUP BY symbol, granularity""", (int(since),))
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["symbol"], {"bars": 0})
        d["bars"] += r["n"]
        if r["granularity"] == 3600 and r["px"]:
            span_h = max((r["b"] - r["a"]) / 3600, 1)
            d["dollar_volume_day"] = float(r["v"] * r["px"] * 24)
            d["completeness_pct"] = min(100.0, 100.0 * r["n"] / span_h)

    for sym in list(out):
        cl = [x["close"] for x in db.query(
            """SELECT close FROM bars WHERE symbol=? AND granularity=3600
               ORDER BY ts DESC LIMIT 500""", (sym,))]
        if len(cl) > 30:
            lr = np.diff(np.log(np.maximum(np.array(cl[::-1]), 1e-12)))
            out[sym]["vol_bps"] = float(np.std(lr) * 1e4)
            out[sym]["daily_range_pct"] = float(np.std(lr) * np.sqrt(24) * 100)
    return out


def _order_size_usd(days: float = 30.0) -> tuple[float, str]:
    """How big the desk's orders actually are, from the orders it actually placed.

    The old liquidity floor said "our $100 order must be a rounding error in
    $2M a day". Both numbers were typed in: the book is $2,000 now, the median
    open is ~$97 and the largest ~$252, and none of that reached the gate. The
    gate is about OUR order in THEIR market, so the order size is read from the
    orders table -- the 90th percentile of recent opens, because the gate has to
    hold for the large ones, not the typical one. Before any order exists the
    fallback is the declared stake divided by the number of positions the book
    has held at once, which is the only estimate available then, and is labelled.
    """
    try:
        rows = db.query(
            "SELECT notional_usd FROM orders WHERE status='filled' AND intent='open' "
            "AND notional_usd > 0 AND ts_decided >= ? ORDER BY notional_usd",
            (time.time() - days * 86400.0,))
        vals = [float(r["notional_usd"]) for r in rows]
        if len(vals) >= 5:
            return vals[min(len(vals) - 1, int(0.9 * len(vals)))], f"p90 of {len(vals)} opens in {days:.0f}d"
    except Exception:
        pass
    try:
        from app.core import mode as mode_mod
        eq = float(mode_mod.get_equity())
        return max(eq / 10.0, 1.0), "no fills yet: stake / 10"
    except Exception:
        return 100.0, "no fills, no stake: $100 placeholder"


def _impact_gate(dv_day: float, vol_bps_hr: float | None, order_usd: float, cost) -> dict:
    """Would our order move this coin more than we can afford?

    Square-root impact (Almgren-Chriss, and every empirical study since):

        impact_bps ~= sigma_hourly_bps * sqrt(order / hourly_dollar_volume)

    The tolerance is not a number picked here. It is the width of the cost
    model's OWN uncertainty about this coin's cost (ci_high - markup): an impact
    smaller than the error bar on the spread cannot be told apart from noise, so
    it is not a reason to exclude the coin; an impact larger than it is a cost
    the model is not carrying, and the coin is excluded until the book is
    smaller or the coin is deeper. As real fills narrow the CI the gate tightens
    by itself -- which is the right direction for a rule to move as it learns.
    """
    dv_hr = max(dv_day, 0.0) / 24.0
    if dv_hr <= 0 or not vol_bps_hr or not np.isfinite(vol_bps_hr):
        return {"passed": False, "impact_bps": None, "tolerance_bps": None,
                "participation": None}
    part = order_usd / dv_hr
    impact = float(vol_bps_hr) * float(np.sqrt(part))
    # Two bounds, both the coin's own numbers, and the tighter one governs:
    #   * the cost model's error bar (ci_high - markup): below it the impact is
    #     indistinguishable from what we already do not know about the cost;
    #   * the one-side spread itself: an "assumed" coin has an error bar of
    #     500-1,100 bps, and an impact that large would be a second spread.
    #     Uncertainty about a cost is never a licence to add one.
    tol = max(min(float(cost.ci_high_bps) - float(cost.markup_bps),
                  float(cost.markup_bps)), 1.0)
    return {"passed": bool(impact <= tol), "impact_bps": impact, "tolerance_bps": tol,
            "participation": part}


def _score(sym: str, m: dict, cost, cfg: dict, order_usd: float | None = None,
           order_src: str = "") -> tuple[float, list[dict]]:
    """Each gate returns pass/fail plus a 0-1 contribution. The score is the mean
    of the contributions; the gates are what actually decide eligibility."""
    dv = m.get("dollar_volume_day") or 0.0
    if order_usd is None:
        order_usd, order_src = _order_size_usd()
    vol = m.get("vol_bps")
    rt = cost.round_trip_bps
    comp = m.get("completeness_pct") or 0.0
    bars = m.get("bars") or 0

    def band(x, lo, hi):
        if x is None or not np.isfinite(x):
            return 0.0, False
        if x < lo or x > hi:
            return 0.0, False
        mid = (np.log(lo) + np.log(hi)) / 2
        spread = (np.log(hi) - np.log(lo)) / 2
        return float(max(0.0, 1 - abs(np.log(x) - mid) / spread)), True

    imp = _impact_gate(dv, vol, order_usd, cost)
    liq_ok = imp["passed"]
    # Contribution: 1 when our impact is negligible against the tolerance,
    # falling to 0 as it reaches it. Log-scaled so a coin 100x deeper than it
    # needs to be is not scored the same as one that just squeaks through.
    if imp["impact_bps"] is None:
        liq = 0.0
    else:
        ratio = max(imp["impact_bps"], 1e-9) / imp["tolerance_bps"]
        liq = float(np.clip(-np.log10(ratio) / 2.0, 0, 1))   # 1 at 1% of tolerance, 0 at 100%
    old_floor_ok = dv >= cfg["min_dollar_volume"]
    # COST AGAINST THE COIN'S OWN MOVEMENT, not against a typed ceiling.
    #
    # The fixed 2.00% ceiling excluded XTZ (2.12%), BONK (2.19%), XPL (2.01%)
    # and ZORA (2.01%) -- and the daily reports for 09-18 .. 09-21 list XTZ
    # +32.4%, XTZ +21.1%, BONK +9.9%/+10.3%, XPL +9.5%/+8.0%/+7.9% among the
    # moves "no strategy was watching". A coin that moves 5% on a typical day
    # is tradeable at a 2.1% round trip; a coin that moves 1.5% is not
    # tradeable at 1.9%. So the test is: does the coin's typical day
    # (volatility-implied daily range) exceed what a round trip costs? The old
    # ceiling is kept only as a labelled comparison. The per-signal hurdle
    # still judges every trade against the coin's own cost at entry time.
    day_range = float(m.get("daily_range_pct") or 0.0)
    cost_ok = bool(day_range > 0 and (rt / 100.0) <= day_range)
    cost_score = float(np.clip(1 - (rt / 100.0) / max(day_range, 1e-9), 0, 1)) if day_range > 0 else 0.0
    old_cost_ceiling_ok = rt <= cfg["max_round_trip_bps"]
    vol_score, vol_ok = band(vol, cfg["min_vol_bps"], cfg["max_vol_bps"])
    data_ok = bars >= cfg["min_bars"] and comp >= cfg["min_completeness_pct"]
    data_score = float(np.clip(comp / 100, 0, 1))

    gates = [
        {"gate": "liquidity", "passed": bool(liq_ok), "score": liq,
         "actual": (f"${dv/1e6:.1f}M/day; a ${order_usd:.0f} order would move it "
                    f"~{imp['impact_bps']:.0f} bps" if imp["impact_bps"] is not None
                    else f"${dv/1e6:.1f}M/day; impact unknown (no volatility reading)"),
         "required": (f"impact <= {imp['tolerance_bps']:.0f} bps (the cost model's own "
                      f"error bar on this coin)" if imp["tolerance_bps"] is not None
                      else "a volatility and volume reading"),
         "why": ("Our order has to be small enough that its own market impact is lost "
                 "inside the uncertainty of the cost we already pay. Order size is "
                 f"{order_src}; impact is sqrt-law, hourly volatility x sqrt(order / "
                 "hourly dollar volume)."),
         "order_usd": order_usd, "impact_bps": imp["impact_bps"],
         "tolerance_bps": imp["tolerance_bps"], "participation": imp["participation"],
         "old_fixed_floor_would_pass": bool(old_floor_ok)},
        {"gate": "cost", "passed": bool(cost_ok), "score": cost_score,
         "actual": f"{rt/100:.2f}% round trip ({cost.status}) vs a typical day of {day_range:.2f}%",
         "old_fixed_ceiling_would_pass": bool(old_cost_ceiling_ok),
         "required": "round trip <= the coin's typical daily range",
         "why": ("A coin whose spread exceeds the moves it makes cannot be traded profitably -- "
                 "measured against ITS moves, not a ceiling that fits every coin the same.")},
        {"gate": "movement", "passed": bool(vol_ok), "score": vol_score,
         "actual": f"{vol:.0f} bps/hr" if vol else "unknown",
         "required": f"{cfg['min_vol_bps']:.0f}-{cfg['max_vol_bps']:.0f} bps/hr",
         "why": "Too quiet and no strategy can clear the spread; too wild and sizing stops meaning anything."},
        {"gate": "data", "passed": bool(data_ok), "score": data_score,
         "actual": f"{bars:,} bars, {comp:.0f}% complete",
         "required": f">= {cfg['min_bars']:,} bars, {cfg['min_completeness_pct']:.0f}%",
         "why": "Nothing can be fitted or validated on a coin with holes in its history."},
    ]
    score = float(np.mean([g["score"] for g in gates]))
    return score, gates


def review(config: dict | None = None, dry_run: bool = False) -> dict:
    """Re-decide every coin's role. Safe to run daily."""
    ensure_schema()
    cfg = {**DEFAULTS, **(config or {})}
    now = time.time()

    rows = db.query("SELECT * FROM universe")
    metrics = _metrics()
    obs, coef = symbol_cost.observables(), symbol_cost.fit_coefficients()
    synced = db.query_one("SELECT COUNT(*) c FROM universe WHERE rh_confirmed=1")["c"] > 0

    order_usd, order_src = _order_size_usd()
    scored = []
    for r in rows:
        sym = r["symbol"]
        m = metrics.get(sym, {})
        cost = symbol_cost.estimate_symbol(sym, obs, coef)
        score, gates = _score(sym, m, cost, cfg, order_usd, order_src)
        tradable = (bool(r["rh_confirmed"]) if synced else True)
        scored.append({
            "symbol": sym, "score": score, "gates": gates,
            "all_gates_pass": all(g["passed"] for g in gates),
            "rh_tradable": tradable,
            # A coin with no role yet starts at "watch", NOT "tradeable". Defaulting
            # to tradeable meant an unproven coin was treated as an incumbent and
            # hysteresis then protected it for `exit_patience` reviews — the exact
            # opposite of what hysteresis is for.
            "current_role": r["role"] or ("core" if sym in CORE else "watch"),
            "below_exit_count": r["below_exit_count"] or 0,
            "metrics": m, "round_trip_bps": cost.round_trip_bps,
        })

    eligible = [s for s in scored if s["rh_tradable"] and s["all_gates_pass"]]
    eligible.sort(key=lambda s: -s["score"])
    _cap = int(cfg.get("max_tradeable") or 0)
    top = {s["symbol"] for s in (eligible[:_cap] if _cap > 0 else eligible)}

    changes = []
    for s in scored:
        sym, cur = s["symbol"], s["current_role"]
        if sym in CORE:
            new, reason = "core", "major — always tracked so breadth and the leader reading stay valid"
        elif not s["rh_tradable"]:
            new, reason = "watch", "not confirmed tradeable on Robinhood — tracked for context only"
        elif sym in top and s["score"] >= cfg["enter_score"]:
            new, reason = "tradeable", f"score {s['score']:.2f} clears the {cfg['enter_score']} entry bar"
            s["below_exit_count"] = 0
        elif cur == "tradeable":
            # hysteresis: demote only after sustained weakness
            if s["score"] < cfg["exit_score"]:
                s["below_exit_count"] += 1
                if s["below_exit_count"] >= cfg["exit_patience"]:
                    failed = [g["gate"] for g in s["gates"] if not g["passed"]]
                    new = "watch"
                    reason = (f"score {s['score']:.2f} below the {cfg['exit_score']} exit bar for "
                              f"{s['below_exit_count']} reviews"
                              + (f"; failing {', '.join(failed)}" if failed else ""))
                else:
                    new, reason = "tradeable", (
                        f"score {s['score']:.2f} is weak but held — "
                        f"{s['below_exit_count']}/{cfg['exit_patience']} reviews before demotion")
            else:
                s["below_exit_count"] = 0
                new, reason = "tradeable", f"score {s['score']:.2f} above the {cfg['exit_score']} exit bar"
        else:
            # Say WHICH gate actually failed.
            #
            # This used to report "did not make the top N" for every coin it did
            # not promote, whatever the real cause -- so HYPE, with a 1.92% round
            # trip and $44M a day, was labelled a ranking casualty when the truth
            # was that it has 336 hourly bars and fails the data gate. That
            # message sent two people hunting the wrong problem. The ranking is
            # the reason ONLY when the coin passed every gate and still lost a
            # seat; otherwise name the gate.
            failed = [g for g in s["gates"] if not g["passed"]]
            if failed:
                new = "watch"
                reason = "; ".join(f"{g['gate']}: {g['actual']} (needs {g['required']})"
                                   for g in failed)
            elif s["score"] < cfg["enter_score"]:
                new = "watch"
                reason = f"score {s['score']:.2f} below the {cfg['enter_score']} entry bar"
            else:
                new = "watch"
                reason = (f"score {s['score']:.2f} passed every gate but lost a seat — "
                          f"only {cfg['max_tradeable']} are held at once"
                          if _cap > 0 else
                          f"score {s['score']:.2f} passed every gate")

        s["new_role"], s["reason"] = new, reason
        if new != cur:
            changes.append({"symbol": sym, "from": cur, "to": new,
                            "score": s["score"], "reason": reason})

        if not dry_run:
            db.execute(
                """UPDATE universe SET role=?, score=?, last_reviewed=?, below_exit_count=?,
                   active=?, selection_json=? WHERE symbol=?""",
                (new, s["score"], now, s["below_exit_count"],
                 0 if new == "excluded" else 1, json.dumps(s["gates"]), sym))
            if new != cur:
                db.execute(
                    """INSERT INTO universe_history(ts, symbol, from_role, to_role, score, reason)
                       VALUES (?,?,?,?,?,?)""", (now, sym, cur, new, s["score"], reason))

    if changes and not dry_run:
        db.log_event("INFO", "universe",
                     f"selection review: {len(changes)} role changes",
                     {"changes": changes[:20]})

    scored.sort(key=lambda s: (-s["score"]))
    # The change is only worth making if it is measurable: how many coins does
    # the impact gate admit that the old $2M floor refused, and the reverse?
    liq_rows = [next(g for g in s["gates"] if g["gate"] == "liquidity") for s in scored]
    widened = [s["symbol"] for s, g in zip(scored, liq_rows)
               if g["passed"] and not g.get("old_fixed_floor_would_pass")]
    narrowed = [s["symbol"] for s, g in zip(scored, liq_rows)
                if not g["passed"] and g.get("old_fixed_floor_would_pass")]
    return {
        "reviewed_at": now,
        "config": cfg,
        "liquidity_gate": {
            "order_usd": order_usd, "order_source": order_src,
            "admitted_that_old_floor_refused": widened,
            "refused_that_old_floor_admitted": narrowed,
            "how": ("impact = hourly volatility x sqrt(order / hourly $ volume); passes when "
                    "impact <= the cost model's CI width for that coin"),
        },
        "robinhood_synced": synced,
        "counts": {
            "core": sum(1 for s in scored if s["new_role"] == "core"),
            "tradeable": sum(1 for s in scored if s["new_role"] == "tradeable"),
            "watch": sum(1 for s in scored if s["new_role"] == "watch"),
        },
        "changes": changes,
        "rows": scored,
        "dry_run": dry_run,
        "note": ("Roles are decided by the gates, not by a hand-written list. "
                 + ("Robinhood tradability has been synced, so untradeable coins are "
                    "demoted to watch automatically."
                    if synced else
                    "Robinhood has NOT been synced yet, so every coin is assumed tradeable. "
                    "Run the sync in docs/ROBINHOOD_SYNC.md — until then a coin like FIL, "
                    "which is not on Robinhood, will still be treated as tradeable.")),
    }


def tradeable_symbols() -> list[str]:
    ensure_schema()
    rows = db.query(
        "SELECT symbol FROM universe WHERE role IN ('core','tradeable') AND active=1 ORDER BY symbol")
    # This used to fall back to the ENTIRE active universe when no coin had a
    # role yet -- which is the state of a fresh or freshly-restored database,
    # since `role` is an added column with no default. That silently made every
    # watched coin tradeable, including the ones deliberately held back.
    # No tradeable coin is the safe answer; the engine already handles an empty set.
    return [r["symbol"] for r in rows]


def tracked_symbols() -> list[str]:
    ensure_schema()
    return [r["symbol"] for r in db.query(
        "SELECT symbol FROM universe WHERE active=1 ORDER BY symbol")]


def history(limit: int = 100) -> list[dict]:
    ensure_schema()
    return db.query("SELECT * FROM universe_history ORDER BY ts DESC LIMIT ?", (limit,))
