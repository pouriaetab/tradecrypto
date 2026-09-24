"""The plan is written at entry and graded as it runs (2026-09-23)."""
import pytest

from app.research.trade_plan import (SHAPE_TABLE, build, grade, live, mark,
                                     record, wobble_floor_pct)


def _plan():
    return build("DOGE", "day_climb", 0.24, 0.24 * 1.0361, 0.24 * 0.92, 0.0258,
                 why={"rank_this_bar": 1, "signals_this_bar": 14})


def test_the_plan_states_a_price_a_deadline_and_the_odds():
    p = _plan()
    assert p["target_px"] > p["entry_px"]
    assert p["window_h"] > 0
    assert 0 < p["p_reach"] <= 1
    assert "should reach" in p["prediction"]
    assert p["how_measured"]


def test_the_expected_shape_is_attached_and_sums_to_everything():
    p = _plan()
    sh = p["shape"]
    assert sum(sh.values()) == pytest.approx(100, abs=2), \
        "the four path outcomes must account for every entry"
    assert p["expected_dip_pct"] < 0, "a dip we expect is a fall, not a rise"


def test_a_harder_target_is_told_to_expect_a_worse_outcome():
    easy = build("X", "s", 100.0, 104.0, 92.0, 0.030)
    hard = build("X", "s", 100.0, 104.0, 92.0, 0.004)
    assert easy["p_reach"] > hard["p_reach"]
    assert easy["window_h"] < hard["window_h"]
    assert easy["shape"]["never_reached_pct"] < hard["shape"]["never_reached_pct"]


def test_the_four_states_the_operator_asked_for():
    p = _plan()
    assert grade(p, 2.0, -1.0, 0.5, -0.5, False)[0] == "on_track"
    # "we should have tagged this even live as a possibly failed signal before 4 hrs"
    assert grade(p, 3.0, -9.0, 0.0, -8.0, False)[0] == "wobbling"
    # "then after 4 hrs as a failed wrong prediction"
    assert grade(p, 30.0, -4.0, 1.0, -1.2, False)[0] == "failed"
    # "there was a brief opportunity to get out and close and move on"
    assert grade(p, 30.0, -4.0, 1.0, +0.4, False)[0] == "exit_seeking"
    assert grade(p, 5.0, -1.0, 4.0, 3.9, True)[0] == "on_track"


def test_wobble_scales_with_the_shape_not_a_fixed_percentage():
    """A coin whose normal path dips 3.4% must not be judged by the same
    absolute number as one that dips 1.4%."""
    a = build("X", "s", 100.0, 104.0, 92.0, 0.030)
    b = build("X", "s", 100.0, 104.0, 92.0, 0.004)
    assert wobble_floor_pct(a) != wobble_floor_pct(b)
    assert wobble_floor_pct(a) < 0


def test_the_plan_is_written_once_and_not_rewritten(writable_db):
    """A prediction you can still edit is not a prediction."""
    p = _plan()
    record(p, "paper", 1000.0)
    p2 = dict(p, target_px=p["target_px"] * 2, p_reach=0.99)
    record(p2, "paper", 1000.0)
    rows = live("paper")
    assert len(rows) == 1
    assert rows[0]["target_px"] == pytest.approx(p["target_px"])
    assert rows[0]["p_reach"] == pytest.approx(p["p_reach"])


def test_mark_reports_only_real_changes(writable_db):
    record(_plan(), "paper", 1000.0)
    assert mark("DOGE", "day_climb", "paper", 1000.0, "wobbling", "broke early", -9.0, 0.0)
    assert not mark("DOGE", "day_climb", "paper", 1000.0, "wobbling", "still broken", -9.5, 0.0)
    assert mark("DOGE", "day_climb", "paper", 1000.0, "failed", "window gone", -9.5, 0.0)
    row = live("paper")[0]
    assert row["state"] == "failed"
    assert [n["state"] for n in row["notes"]] == ["wobbling", "failed"]


def test_why_this_signal_was_promoted_is_kept(writable_db):
    record(_plan(), "paper", 1000.0)
    why = live("paper")[0]["why"]
    assert why["rank_this_bar"] == 1 and why["signals_this_bar"] == 14


def test_backfill_uses_only_what_existed_before_the_fill(writable_db):
    """THE HONESTY PROPERTY of the backfill. If it reads bars from after the
    entry, the 'prediction' is hindsight and every accuracy number is a lie."""
    import inspect
    from app.research import trade_plan
    src = inspect.getsource(trade_plan._range_before)
    assert "ts < ?" in src, "the reconstructed range can see bars from after the fill"
    assert "ORDER BY ts DESC" in src


def test_every_backfilled_plan_says_it_was_rebuilt(writable_db):
    import inspect
    from app.research import trade_plan
    src = inspect.getsource(trade_plan._why_from_signals)
    assert '"reconstructed": True' in src
    # both branches -- with and without a matching signal
    assert src.count('"reconstructed": True') == 2


def test_accuracy_compares_what_happened_to_what_was_promised(writable_db):
    from app.research.trade_plan import accuracy, mark, record
    p = _plan()
    record(p, "paper", 1000.0)
    mark("DOGE", "day_climb", "paper", 1000.0, "on_track", "hit it", -1.0, 4.0)
    rows = accuracy("paper")
    assert len(rows) == 1
    r = rows[0]
    assert r["n"] == 1 and r["on_track"] == 1
    assert r["hit_rate"] == pytest.approx(100.0)
    assert r["predicted_rate"] == pytest.approx(p["p_reach"] * 100.0)
    assert r["calibration_gap"] == pytest.approx(r["hit_rate"] - r["predicted_rate"])


def test_only_a_failed_trade_can_be_closed_for_being_green(writable_db):
    """THE DISTINCTION THAT MAKES THIS RULE WORK. 'Past 20h and green, take it'
    lost 3 pts/trade because it capped winners too. This may fire only on a
    trade that has already failed its own prediction."""
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    # Re-pointed 2026-09-23: a second branch (`recovered`) now also closes a
    # position, so anchoring on "the 300 characters before the first _plan_exit"
    # no longer describes the rule. The INVARIANT is what matters and it is
    # unchanged: every branch that closes a position must be guarded by a state
    # that means the plan went wrong. A healthy trade must never be capped --
    # that mistake cost 3 pts/trade in session 66.
    closers = [j for j in range(len(src)) if src.startswith('pos["_plan_exit"]', j)]
    assert closers, "nothing closes a position on its plan any more"
    for j in closers:
        cond = src[max(0, j - 400):j]
        assert ('state == "exit_seeking"' in cond) or ('state == "recovered"' in cond), \
            "a close fires on a state that does not mean the plan went wrong"
        assert ("EXIT_SEEKING_CLOSES" in cond) or ("RECOVERY_CLOSES" in cond), \
            "a close has no switch to turn it off"
    # and the failed-and-green branch still refuses to sell at a loss
    k = src.index('state == "exit_seeking"')
    assert "now_pct > 0" in src[k:k + 200], "it would close a failed trade at a loss"


def test_the_plan_exit_is_checked_before_the_stop(writable_db):
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index('reason = None')
    blk = src[i:i + 400]
    assert blk.index("_plan_exit") < blk.index("stop_px"), \
        "the stop would fire first and the green moment would be missed"


def test_a_trade_that_broke_its_dip_and_came_back_is_taken(writable_db):
    """The operator, reading the live book: "when realized we got lucky after
    bad predicted entry ... we can do breakeven or little bit profit to just
    exit". OP fell 5.06% where its shape dips 3.1%, then came back to +1.24%."""
    from app.research.trade_plan import grade
    p = build("OP", "day_climb", 100.0, 103.12, 92.0, 0.0207)
    assert grade(p, 8.0, -5.06, 1.24, 1.24, False)[0] == "recovered"
    assert grade(p, 8.0, -5.58, 1.93, 1.93, False)[0] == "recovered"


def test_it_does_not_fire_when_nothing_went_wrong(writable_db):
    """A shallow dip means the entry was fine. Selling at +0.60% there is the
    cap-the-winner mistake that cost 3 pts/trade in session 66."""
    from app.research.trade_plan import grade
    p = build("OP", "day_climb", 100.0, 103.12, 92.0, 0.0207)
    assert grade(p, 8.0, -1.00, 0.60, 0.60, False)[0] != "recovered"


def test_it_does_not_fire_while_still_losing(writable_db):
    from app.research.trade_plan import grade
    p = build("OP", "day_climb", 100.0, 103.12, 92.0, 0.0207)
    assert grade(p, 8.0, -5.06, 0.0, -0.20, False)[0] != "recovered"


def test_the_recovery_exit_does_not_need_the_window_to_expire(writable_db):
    """THE WHOLE POINT. On all 9 open positions that were ever green, the green
    arrived INSIDE the window, so the window-based exit never fired once."""
    from app.research.trade_plan import grade
    p = build("OP", "day_climb", 100.0, 103.12, 92.0, 0.0207)
    young = grade(p, 2.0, -5.06, 1.24, 1.24, False)      # 2h, window is 12h+
    assert young[0] == "recovered", "the recovery exit is gated on the clock again"


def test_the_engine_closes_on_recovery(writable_db):
    import inspect
    from app.execution import engine
    src = inspect.getsource(engine)
    i = src.index('state == "recovered"')
    assert "RECOVERY_CLOSES" in src[i:i + 200], "there is no switch to turn it off"
    assert '_plan_exit' in src[i:i + 200], "the state is set but nothing closes"
