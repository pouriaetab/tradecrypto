"""Built is not the same word as running.

    "you actually confirm that the trailing stop has changed multiple times for
     one stock. And now you don't have a memory of it in the web app."

The ratchet code was real and had never once been entered: it was gated on
`trail_bps`, which every strategy but one leaves at 0. The claim was made from
reading the code rather than from evidence that the code had run — and nothing
in this project could have told the difference. Tests passed (they call the
function directly). The gate passed (the code is present). The UI showed the
setting as on.

Same shape as "you said the strategy was in place and days later we found it
never went live". One question catches both: HAS THIS EVER FIRED?
"""
from __future__ import annotations

import time

import pytest


class TestTheCounter:

    def test_firing_is_recorded(self, writable_db):
        from app.core import liveness
        liveness.fired("stop_ratchet", "AVAX 7.33 -> 7.90")
        r = liveness.report()
        m = next(x for x in r["mechanisms"] if x["name"] == "stop_ratchet")
        assert m["times"] == 1 and m["never"] is False

    def test_a_mechanism_that_never_fires_is_reported_by_name(self, writable_db):
        from app.core import db, liveness
        # An old order, so the desk is demonstrably past every grace period.
        db.execute("INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
                   "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("t1", "day_climb", "AVAX", "buy", "open", 1.0, 10.0, "paper",
                    "filled", time.time() - 40 * 24 * 3600))
        # stop_ratchet is DECLARED not-live, so it is a note. Use one that is not.
        liveness.REGISTRY["_probe_undeclared"] = {
            "what": "a test mechanism nobody declared", "grace_s": 1, "why": "test"}
        try:
            r = liveness.report()
            assert "_probe_undeclared" in r["never_fired"], r["headline"]
            assert r["ok"] is False
            assert "_probe_undeclared" in r["headline"]
            # ...and the declared one is reported, but as a known hole.
            assert "stop_ratchet" in r["known_not_live"]
            assert "stop_ratchet" not in r["never_fired"]
        finally:
            liveness.REGISTRY.pop("_probe_undeclared", None)

    def test_the_counter_can_never_break_the_thing_it_counts(self, monkeypatch):
        """A mechanism must not fail because its odometer failed."""
        from app.core import db, liveness
        monkeypatch.setattr(db, "execute",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        liveness.fired("stop_ratchet")          # must not raise

    def test_a_young_desk_is_not_accused(self, writable_db):
        """Nothing can be blamed for not firing before there was anything to fire on."""
        from app.core import db, liveness
        db.execute("INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
                   "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("t2", "day_climb", "AVAX", "buy", "open", 1.0, 10.0, "paper",
                    "filled", time.time() - 60))
        r = liveness.report()
        assert r["never_fired"] == [], "a one-minute-old desk was accused of not ratcheting"

    def test_report_needs_no_write_access(self, writable_db, monkeypatch):
        """It runs from the gate, which opens the database immutable. A checker
        that needs write access to report cannot run where it is most needed."""
        from app.core import db, liveness
        monkeypatch.setattr(liveness, "ensure_schema",
                            lambda: (_ for _ in ()).throw(RuntimeError("read-only")))
        r = liveness.report()
        assert "mechanisms" in r and r.get("error") is None


class TestSeedingFromHistory:
    """A check whose first report is mostly noise is a check that gets ignored."""

    def test_existing_trades_and_orders_seed_their_counters(self, writable_db):
        from app.core import db, liveness
        now = time.time()
        for i in range(3):
            db.execute("INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
                       "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (f"s{i}", "day_climb", "AVAX", "buy", "open", 1.0, 10.0, "paper",
                        "filled", now - 40 * 24 * 3600))
        liveness.seed_from_history()
        r = liveness.report()
        m = next(x for x in r["mechanisms"] if x["name"] == "order_placed")
        assert m["times"] == 3, "orders that already happened still read as never placed"

    def test_seeding_never_lowers_a_count(self, writable_db):
        from app.core import db, liveness
        liveness.fired("order_placed"); liveness.fired("order_placed")
        db.execute("INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
                   "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("one", "day_climb", "AVAX", "buy", "open", 1.0, 10.0, "paper",
                    "filled", time.time()))
        liveness.seed_from_history()
        m = next(x for x in liveness.report()["mechanisms"] if x["name"] == "order_placed")
        assert m["times"] >= 2, "seeding overwrote a real count with a smaller one"

    def test_the_ratchet_is_never_seeded(self, writable_db):
        """There is no historical evidence for it, because it never happened.
        Inventing a count here would hide the exact thing this exists to find."""
        import inspect
        from app.core import liveness
        src = inspect.getsource(liveness.seed_from_history)
        assert "stop_ratchet" not in src


class TestItIsWiredIn:

    def _src(self, rel):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / rel).read_text()

    def test_the_ratchet_reports_when_it_moves_a_stop(self):
        s = self._src("app/execution/engine.py")
        assert 'liveness.fired("stop_ratchet"' in s

    def test_the_breakeven_lock_reports(self):
        assert 'liveness.fired("stop_locked_breakeven"' in self._src("app/execution/engine.py")

    def test_orders_and_closes_report(self):
        assert 'liveness.fired("order_placed"' in self._src("app/execution/broker.py")
        assert 'liveness.fired("trade_closed"' in self._src("app/execution/engine.py")

    def test_boot_says_out_loud_what_has_never_run(self):
        s = self._src("app/main.py")
        assert "seed_from_history()" in s and "NEVER RUN" in s

    def test_the_vault_write_path_stays_independent(self):
        """The vault must not import from app.* to record its own liveness —
        that is exactly the coupling it exists to avoid, and adding this counter
        broke it once already."""
        import inspect
        from app.core import vault
        assert "liveness" not in inspect.getsource(vault.record)


class TestADeclaredHoleIsNotAFalseAlarm:
    """A gate that is permanently red gets switched off (blueprint 5.1). A hole
    nobody can see is indistinguishable from the bug (4.2). So: a mechanism that
    is knowingly not live is DECLARED, with a reason and a date, and reported as
    a named note rather than a failure."""

    def test_the_declaration_carries_a_reason(self):
        from app.core import liveness
        for name in ("stop_ratchet", "stop_locked_breakeven"):
            d = liveness.REGISTRY[name].get("known_not_live")
            assert d and len(d) > 40, f"{name} is declared with no explanation"
            assert "2026-" in d, f"{name}'s declaration is undated"

    def test_an_undeclared_silence_is_still_a_failure(self, writable_db):
        """The declaration must not become a way to silence everything."""
        import time
        from app.core import db, liveness
        db.execute("INSERT INTO orders (client_id, strategy, symbol, side, intent, qty, "
                   "notional_usd, mode, status, ts_decided) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("old", "day_climb", "AVAX", "buy", "open", 1.0, 10.0, "paper",
                    "filled", time.time() - 40 * 24 * 3600))
        undeclared = [n for n, sp in liveness.REGISTRY.items()
                      if not sp.get("known_not_live")]
        assert undeclared, "every mechanism has been declared away"
        r = liveness.report()
        assert r["never_fired"], "nothing can fail any more"
