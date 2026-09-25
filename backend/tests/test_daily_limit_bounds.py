"""The one risk limit an operator can move while the desk runs, and its bounds.

`set_daily_loss_pct` shipped without a test. It deserves one more than most of
this system, for two reasons.

It is the only risk limit here that can be changed without editing a file and
restarting, which makes it the one most likely to actually be changed. And its
failure mode is silent in both directions: accept 0 and the desk halts on the
first cent of loss, accept 500 and there is no cap at all. Either way the
application keeps running, every screen looks normal, and the number on the Risk
page is a number.

`docs/ENGINEERING.md` §3 cites these bounds as a control with a stated
acceptance criterion. A cited control with no test is a claim, not a control, so
this file is the boundary-value case set it was missing: both edges, just
outside both edges, and the two degenerate values that are the real failure
modes.

The read path is tested as well as the write path. A bound enforced only on the
way in is one hand-edited row away from being no bound at all.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core import db
from app.risk import guards


# ── inside the range ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct", [guards.PCT_MIN, 1.0, 3.0, 25.0, guards.PCT_MAX])
def test_values_inside_the_range_are_accepted(writable_db, pct):
    """Both edges are included on purpose. A bound you cannot select is not the
    bound you documented."""
    out = guards.set_daily_loss_pct(pct)
    assert out["pct"] == pytest.approx(pct)
    assert out["source"] == "operator"
    assert guards.daily_loss_pct() == pytest.approx(pct)


# ── outside the range ────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct", [
    guards.PCT_MIN - 0.01,   # just below the floor
    guards.PCT_MAX + 0.01,   # just above the ceiling
    0.0,                     # halts on the first cent
    -5.0,                    # a negative cap is not a cap
    1000.0,                  # no cap at all
])
def test_values_outside_the_range_are_refused(writable_db, pct):
    with pytest.raises(ValueError):
        guards.set_daily_loss_pct(pct)


def test_the_refusal_names_both_bounds(writable_db):
    """An operator told only "invalid" twice stops using the control and goes
    back to editing .env by hand — which is the behaviour this feature existed
    to end. The message has to say what the allowed range actually is."""
    with pytest.raises(ValueError) as exc:
        guards.set_daily_loss_pct(guards.PCT_MAX + 1)
    msg = str(exc.value)
    assert str(guards.PCT_MIN) in msg
    assert str(guards.PCT_MAX) in msg


def test_a_refused_value_never_becomes_the_limit(writable_db):
    """Validation must happen before the write. If it happened after, a refused
    value would already be the cap by the time the error was raised."""
    guards.set_daily_loss_pct(4.0)
    with pytest.raises(ValueError):
        guards.set_daily_loss_pct(0.0)
    assert guards.daily_loss_pct() == pytest.approx(4.0)


# ── the read path ────────────────────────────────────────────────────────────

def test_clearing_the_override_falls_back_to_the_file(writable_db):
    """The .env value stays the documented default rather than being
    overwritten by a click, so clearing has to restore it exactly."""
    guards.set_daily_loss_pct(7.5)
    assert guards.daily_loss_pct() == pytest.approx(7.5)

    out = guards.set_daily_loss_pct(None)
    assert out["source"] == "env"
    assert guards.daily_loss_pct() == pytest.approx(
        get_settings().max_daily_loss_pct)


@pytest.mark.parametrize("stored", ["999", "0", "-1", "not a number", ""])
def test_an_out_of_range_stored_value_is_ignored_on_read(writable_db, stored):
    """The bound is enforced on the way OUT as well as on the way in.

    A row edited by hand, restored from a backup taken when the bounds were
    different, or salvaged out of a corrupted database must not silently become
    the day's cap. It falls back to the file instead.
    """
    guards.set_daily_loss_pct(4.0)
    db.execute("UPDATE app_state SET value=? WHERE key=?",
               (stored, guards._PCT_KEY))
    assert guards.daily_loss_pct() == pytest.approx(
        get_settings().max_daily_loss_pct)


# ── audit ────────────────────────────────────────────────────────────────────

def test_moving_the_limit_is_recorded(writable_db):
    """A risk limit that can be moved without leaving a trace is not a control.

    The event is logged at WARNING rather than INFO deliberately: changing a
    risk limit should be findable later without knowing to look for it.
    """
    guards.set_daily_loss_pct(9.0)
    rows = db.query("SELECT level, category, message FROM events "
                    "WHERE category='risk' ORDER BY id DESC LIMIT 5")
    hits = [r for r in rows if "9.0" in r["message"]]
    assert hits, f"no risk event recorded the change; saw {[r['message'] for r in rows]}"
    assert hits[0]["level"] == "WARNING"
