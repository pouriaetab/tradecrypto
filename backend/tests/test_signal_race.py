"""The field a winning signal beat, and how it was scored (2026-09-23)."""
import pytest

from app.research.signal_race import SCORE_RECIPE, races, recipe


def test_every_active_strategy_says_how_it_scores():
    """A strategy that cannot explain its own ranking is a black box on a page
    whose whole purpose is transparency."""
    from app.strategy.registry import ACTIVE_STRATEGIES
    missing = sorted(set(ACTIVE_STRATEGIES) - set(SCORE_RECIPE))
    assert not missing, f"no scoring recipe for {missing}"


def test_a_recipe_names_its_inputs_and_their_weights():
    for name, r in SCORE_RECIPE.items():
        assert r["expr"] and "raw_score" in r["expr"], f"{name} has no formula"
        assert r["inputs"], f"{name} lists no inputs"
        for inp in r["inputs"]:
            assert inp["name"] and inp["weight"] and inp["what"], \
                f"{name}: an input is missing its weight or its meaning"
        assert r["source"], f"{name} does not say which file the formula is in"


def test_an_unknown_strategy_admits_it_rather_than_inventing_one():
    r = recipe("something_new")
    assert r["known"] is False
    assert r["expr"] is None
    assert "not written down" in r["plain"]


def test_a_race_is_one_strategy_in_one_bar(writable_db):
    from app.core import db
    for sym, score, dec in (("AAA", 9.0, "taken"), ("BBB", 5.0, "rejected"),
                            ("CCC", 7.0, "rejected")):
        db.execute("INSERT INTO signals(ts,strategy,strategy_version,symbol,side,raw_score,"
                   "expected_edge_bps,edge_ci_low_bps,edge_ci_high_bps,cost_hurdle_bps,"
                   "decision,reject_reason,features_json,sample_size) "
                   "VALUES (100,'day_climb','0.1',?,'buy',?,0,0,0,0,?,NULL,'{}',0)",
                   (sym, score, dec))
    # a different strategy in the same second is a DIFFERENT race
    db.execute("INSERT INTO signals(ts,strategy,strategy_version,symbol,side,raw_score,"
               "expected_edge_bps,edge_ci_low_bps,edge_ci_high_bps,cost_hurdle_bps,"
               "decision,reject_reason,features_json,sample_size) "
               "VALUES (100,'volume_build','0.1','ZZZ','buy',99,0,0,0,0,'rejected',NULL,'{}',0)")
    out = races("paper", 10)
    assert len(out) == 1, "two strategies in one second must not be one race"
    r = out[0]
    assert r["strategy"] == "day_climb" and r["candidates"] == 3
    assert [f["symbol"] for f in r["field"]] == ["AAA", "CCC", "BBB"], "not ranked by score"
    assert r["winners"] == ["AAA"] and r["winner_rank"] == 1
    assert r["recipe"]["known"] is True


def test_the_losers_are_kept_with_their_reason(writable_db):
    from app.core import db
    db.execute("INSERT INTO signals(ts,strategy,strategy_version,symbol,side,raw_score,"
               "expected_edge_bps,edge_ci_low_bps,edge_ci_high_bps,cost_hurdle_bps,"
               "decision,reject_reason,features_json,sample_size) "
               "VALUES (200,'day_climb','0.1','WIN','buy',9,0,0,0,0,'taken',NULL,'{}',0)")
    db.execute("INSERT INTO signals(ts,strategy,strategy_version,symbol,side,raw_score,"
               "expected_edge_bps,edge_ci_low_bps,edge_ci_high_bps,cost_hurdle_bps,"
               "decision,reject_reason,features_json,sample_size) "
               "VALUES (200,'day_climb','0.1','LOSE','buy',1,0,0,0,0,'rejected','too extended','{}',0)")
    field = races("paper", 10)[0]["field"]
    loser = next(f for f in field if f["symbol"] == "LOSE")
    assert loser["won"] is False
    assert loser["reject_reason"] == "too extended"
