"""The tests that would have caught the NULL linkage.

    "on this 'trades.open_order_id was NULL on all 16 rows'; how come you missed
     this. please put in tests on these so we dont have these types of issues
     anymore, create tests so these wont happen again"

WHY THE EXISTING GATES DID NOT CATCH IT
---------------------------------------
Every check this repo had was a check on the CODE: compileall, pyflakes, JSX
imports, CSS variables, a render pass. All of them were green the whole time. The
linkage code existed, read correctly, and produced NULL for four days.

The gap is a category, not an oversight in one file: nothing ever inspected the
DATA the running system produces. A unit test cannot catch it either, because
there is no function whose return value is wrong -- the failure only exists once
rows have been written.

So these tests assert POST-CONDITIONS on the database. They are deliberately
tolerant of an empty or fresh database (a new install has no trades, and a gate
that screams on day one is a gate nobody reads by day three) and intolerant of
data that exists and is internally inconsistent.

The same functions run as a scheduled job, because a test that only runs when
somebody types `pytest` would not have caught this one either: the bad rows
accumulated over four days while every gate stayed green.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research import invariants          # noqa: E402
from conftest import skip_without_history


# Invariants that read bars/quotes: absent from a ledger snapshot, present live.
NEEDS_MARKET_DATA = {"prices_on_screen_are_fresh"}


@pytest.mark.parametrize("check", invariants.CHECKS, ids=lambda f: f.__name__)
def test_invariant_holds(check):
    """Each invariant is its own test so a failure names the broken statement."""
    skip_without_history()
    from conftest import skip_without
    if check.__name__ in NEEDS_MARKET_DATA:
        skip_without("bars", "quotes")
    r = check()
    assert r["status"] != "broken", (
        f"\n  INVARIANT BROKEN: {r['name']}"
        f"\n  question: {r['question']}"
        f"\n  found:    {r['detail']}"
        f"\n  this is the class of bug it exists for: {r['would_have_caught']}"
    )


def test_every_invariant_declares_what_it_would_have_caught():
    """A check with no worked example behind it tends to be a guess.

    Each entry has to name a real failure from this project's history, so the
    list stays a record of what actually went wrong rather than a wishlist.
    """
    skip_without_history()
    from conftest import has_table
    market = has_table("bars") and has_table("quotes")
    for check in invariants.CHECKS:
        if check.__name__ in NEEDS_MARKET_DATA and not market:
            continue                    # covered by the parametrised skip above
        r = check()
        assert r["would_have_caught"].strip(), f"{r['name']} has no worked example"
        assert r["question"].strip().endswith("?"), (
            f"{r['name']}'s question is not phrased as a question the operator "
            f"can answer by looking")


def test_run_all_reports_a_headline():
    skip_without_history()
    out = invariants.run_all()
    assert "headline" in out and out["headline"]
    assert len(out["results"]) == len(invariants.CHECKS)
