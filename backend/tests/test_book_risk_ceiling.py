"""The book may never be positioned to lose more than the desk can survive.
(Was the daily cap for one day; at $60 it refused 32 entries with 11 positions.
Now the DRAWDOWN budget, 10% of equity, less today's realised loss.)

2026-09-20 12:00: nine day_climb signals in one bar, all funded. 19 positions,
$1,244 deployed, $108.87 at risk if every stop fired -- against a $60 daily cap
that counts only REALISED losses, so nothing could have stopped it. Cash was
the only limiter. Now: open risk + this order's risk <= the day's remaining
loss headroom, checked in the strategy's own rank order, so the best-ranked
signals are funded and the rest are refused with the reason.
"""
import time

from app.core import db, mode as mode_mod
from app.risk import guards


def _book(writable_db, equity=2000.0, positions=(), realised_today=0.0):
    mode_mod._ensure()
    mode_mod.set_equity(equity)
    db.execute("DELETE FROM positions"); db.execute("DELETE FROM trades"); db.execute("DELETE FROM equity_curve")
    now = time.time()
    deployed = 0.0
    for sym, notional, stop_frac in positions:
        px = 1.0
        qty = notional / px
        db.execute("INSERT INTO positions(symbol, mode, strategy, qty, avg_px, opened_ts, stop_px) "
                   "VALUES (?,?,?,?,?,?,?)", (sym, "paper", "day_climb", qty, px, now, px * (1 - stop_frac)))
        deployed += notional
    db.execute("INSERT INTO equity_curve(ts, mode, equity, cash, positions_value, realised_pnl, unrealised_pnl) "
               "VALUES (?,?,?,?,?,?,?)", (now, "paper", equity, equity - deployed, deployed, 0.0, 0.0))
    if realised_today:
        db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
                   "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) VALUES ('X','day_climb','paper',1,1,1,?,?,60,0,0,?)",
                   (now - 3600, now - 60, realised_today))


def _ceiling(decision):
    return next(c for c in decision.checks if c["check"] == "book_risk_ceiling")


def test_the_burst_is_cut_at_the_ceiling_not_at_the_cash(writable_db):
    # $2,000 book, 10% drawdown budget = $200. Thirty $80 positions with 8%
    # stops risk $192. An $80 order adds $6.40 -> $198.40, passes; a $120
    # order adds $9.60 -> $201.60 and is refused, with the reason.
    _book(writable_db, positions=[(f"C{i}", 80.0, 0.08) for i in range(30)], equity=2000.0)
    ok_ = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert _ceiling(ok_)["passed"]
    d = guards.pre_trade_check(symbol="NEW2", side="buy", notional_usd=120.0, mode="paper", stop_frac=0.08)
    c = _ceiling(d)
    assert not c["passed"]
    assert abs(c["limit"] - 200.0) < 1e-6 and abs(c["actual"] - 201.6) < 1e-6
    assert "rank" in c["detail"]


def test_yesterdays_nineteen_would_have_been_allowed(writable_db):
    # The 2026-09-20 book: 19 positions, ~$109 at risk, $2,000 equity -> under $200.
    _book(writable_db, positions=[(f"C{i}", 65.0, 0.088) for i in range(19)])
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert _ceiling(d)["passed"]


def test_within_the_ceiling_the_order_passes(writable_db):
    _book(writable_db, positions=[(f"C{i}", 80.0, 0.08) for i in range(5)])   # $32 at risk
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    assert _ceiling(d)["passed"]


def test_a_loss_already_taken_today_shrinks_the_headroom(writable_db):
    # $150 lost today -> $50 of the $200 budget left; eight $80 positions risk $51.20.
    _book(writable_db, positions=[(f"C{i}", 80.0, 0.08) for i in range(8)], realised_today=-150.0)
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=80.0, mode="paper", stop_frac=0.08)
    c = _ceiling(d)
    assert not c["passed"] and abs(c["limit"] - 50.0) < 1e-6


def test_an_order_without_a_stop_counts_at_full_notional(writable_db):
    _book(writable_db)
    d = guards.pre_trade_check(symbol="NEW", side="buy", notional_usd=250.0, mode="paper", stop_frac=None)
    assert not _ceiling(d)["passed"], "$250 with no stop is $250 of risk against a $200 budget"


def test_neither_the_same_strategy_nor_a_second_one_may_enter_a_held_coin(writable_db):
    """This asserted the OPPOSITE until 2026-09-22, and correctly so at the time:
    a second strategy WAS allowed into a coin another held, on the reasoning that
    a pump rule and a climb rule are different ideas ("treat it as a separate
    trade"). The operator reversed it after watching CHIP get bought by two
    strategies in the same minute and SHIB five minutes apart -- one idea bought
    twice, paying the round trip twice.

    Re-pointed rather than deleted (checklist 5.31). The half this test was
    originally written to protect -- a strategy may never average into its own
    position -- is unchanged and still asserted. The stacking half now asserts
    the pause, which is an EXPERIMENT: `research/coin_stacking.py` replays every
    refused entry, and the evidence can bring it back.
    """
    _book(writable_db, positions=[("AAA", 80.0, 0.08)])           # held by day_climb
    same = guards.pre_trade_check(symbol="AAA", side="buy", notional_usd=80.0, mode="paper",
                                  stop_frac=0.08, strategy="day_climb")
    other = guards.pre_trade_check(symbol="AAA", side="buy", notional_usd=80.0, mode="paper",
                                   stop_frac=0.08, strategy="pump_catch")
    # unchanged: no averaging into your own position
    assert not next(c for c in same.checks if c["check"] == "no_double_entry")["passed"]
    # reversed: a different strategy is now refused too, and says why
    other_stack = next(c for c in other.checks if c["check"] == "coin_stacking")
    assert not other_stack["passed"]
    assert other_stack["actual"] == 1
    assert "coin_stacking" in other_stack["detail"]
