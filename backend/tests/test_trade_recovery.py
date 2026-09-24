"""Trades disappeared, and worse, four row ids were handed out twice.

2026-09-18. The database was restored from a backup eighteen hours old. Three
trades that closed on the 17th (UNI, NEAR, ONDO) went with it. A fourth was
worse than lost: ZEC had really closed at 09:15 on the 17th for $9.03, but the
backup still showed the position open, so the engine closed it again at the next
boot, at the next day's price, and wrote $24.56 against the wrong day.

Then the restore rolled `sqlite_sequence` back to 31, so the next four trades
took ids 32-35 — the ids those four already had. Nothing errored. Trade #33 was
a UNI trade one day and an ARB trade the next.

    id   was (2026-09-17)                          became
    32   ZEC  09-16 13:21 -> 09-17 09:15  +9.03    ZEC  -> 09-18 00:15  +24.56
    33   UNI  09-17 09:04 -> 09-17 15:14  +6.11    ARB  09-17 00:46 -> 09-18  +14.86
    34   NEAR 09-17 14:49 -> 09-17 18:26  +3.90    WLD  09-18 00:15 -> 09-18   +2.56
    35   ONDO 09-17 08:19 -> 09-17 20:17  +4.48    UNI  09-18 00:15 -> 09-18  +10.96

A missing trade is a hole you can see. A reused id is a hole that looks like data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _insert(db, symbol, strategy, ts_open, ts_close, net, tid=None):
    if tid is None:
        db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, "
                   "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) "
                   "VALUES (?,?,'paper',1,1,1,?,?,?,?,0,?)",
                   (symbol, strategy, ts_open, ts_close, ts_close - ts_open, net, net))
    else:
        db.execute("INSERT INTO trades(id, symbol, strategy, mode, qty, entry_px, exit_px, "
                   "ts_open, ts_close, holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) "
                   "VALUES (?,?,?,'paper',1,1,1,?,?,?,?,0,?)",
                   (tid, symbol, strategy, ts_open, ts_close, ts_close - ts_open, net, net))


# ── the id must never be handed out twice ────────────────────────────────────

class TestIdsAreNeverReused:

    def test_a_restore_cannot_hand_a_used_id_to_a_new_trade(self, writable_db):
        """THE ONE THAT MATTERS. Exactly the 2026-09-18 sequence."""
        from app.core import db, setup_ops
        db.init_db()
        for i in range(15, 36):                       # ids 15..35 exist, as they did
            _insert(db, "OLD", "day_climb", 1.75e9 + i, 1.75e9 + i + 1, 1.0, tid=i)
        setup_ops.enforce_trade_id_high_water_mark()  # mark recorded: 35

        # The restore: the database rolls back to id 31, but app_state travels
        # with it and still remembers 35.
        db.execute("DELETE FROM trades WHERE id > 31")
        db.execute("UPDATE sqlite_sequence SET seq=31 WHERE name='trades'")

        out = setup_ops.enforce_trade_id_high_water_mark()
        assert out["bumped"] is True and out["high_water"] == 35

        _insert(db, "NEW", "day_climb", 1.76e9, 1.76e9 + 1, 2.0)
        got = db.query_one("SELECT id FROM trades WHERE symbol='NEW'")["id"]
        assert got > 35, f"id {got} belonged to a real trade on 2026-09-17"

    def test_it_does_nothing_when_no_rollback_happened(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        _insert(db, "A", "day_climb", 1.75e9, 1.75e9 + 1, 1.0)
        setup_ops.enforce_trade_id_high_water_mark()
        out = setup_ops.enforce_trade_id_high_water_mark()
        assert out["bumped"] is False

    def test_the_mark_survives_a_database_with_no_trades_at_all(self, writable_db):
        """A restore far enough back to empty the table still must not reissue ids."""
        from app.core import db, setup_ops
        db.init_db()
        for i in range(1, 21):
            _insert(db, "OLD", "day_climb", 1.75e9 + i, 1.75e9 + i + 1, 1.0, tid=i)
        setup_ops.enforce_trade_id_high_water_mark()
        db.execute("DELETE FROM trades")
        setup_ops.enforce_trade_id_high_water_mark()
        _insert(db, "NEW", "day_climb", 1.76e9, 1.76e9 + 1, 1.0)
        assert db.query_one("SELECT id FROM trades WHERE symbol='NEW'")["id"] > 20


# ── putting the lost trades back ─────────────────────────────────────────────

class TestLostTradeRecovery:

    def _recovery_file(self, trades):
        from app.config import get_settings
        p = Path(get_settings().db_file).parent / "recovered-2026-09-17.json"
        p.write_text(json.dumps({"match_on": ["symbol", "strategy", "ts_open"],
                                 "trades": trades}))
        return p

    def _t(self, symbol, ts_open, ts_close, net):
        return {"symbol": symbol, "strategy": "day_climb", "mode": "paper", "qty": 1.0,
                "entry_px": 1.0, "exit_px": 1.1, "ts_open": ts_open,
                "ts_close": ts_close, "holding_s": ts_close - ts_open,
                "gross": net + 1, "cost": 1.0, "net": net}

    def test_a_destroyed_trade_comes_back(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        self._recovery_file([self._t("UNI", 1.7897e9, 1.7897e9 + 3600, 6.11)])
        out = setup_ops.restore_lost_trades_once()
        assert out["restored"] == 1
        row = db.query_one("SELECT net_pnl_usd, attribution_json FROM trades WHERE symbol='UNI'")
        assert abs(row["net_pnl_usd"] - 6.11) < 1e-6
        assert "salvaged" in (row["attribution_json"] or "")

    def test_running_it_twice_does_not_duplicate(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        self._recovery_file([self._t("UNI", 1.7897e9, 1.7897e9 + 3600, 6.11)])
        setup_ops.restore_lost_trades_once()
        second = setup_ops.restore_lost_trades_once()
        assert second["restored"] == 0
        assert db.query_one("SELECT COUNT(*) c FROM trades WHERE symbol='UNI'")["c"] == 1

    def test_it_matches_on_the_trade_not_the_id(self, writable_db):
        """The ids were reused, so matching on id would corrupt a real trade."""
        from app.core import db, setup_ops
        db.init_db()
        # id 33 now belongs to a DIFFERENT trade, exactly as it did on the 18th.
        _insert(db, "ARB", "volume_build", 1.7896e9, 1.7897e9, 14.86, tid=33)
        self._recovery_file([self._t("UNI", 1.7897e9, 1.7897e9 + 3600, 6.11)])
        setup_ops.restore_lost_trades_once()
        arb = db.query_one("SELECT symbol, net_pnl_usd FROM trades WHERE id=33")
        assert arb["symbol"] == "ARB" and abs(arb["net_pnl_usd"] - 14.86) < 1e-6
        assert db.query_one("SELECT COUNT(*) c FROM trades WHERE symbol='UNI'")["c"] == 1

    def test_a_close_that_never_happened_is_corrected_not_duplicated(self, writable_db):
        """ZEC: same entry, a close invented a day later at the wrong price."""
        from app.core import db, setup_ops
        db.init_db()
        real_close = 1.7897e9
        _insert(db, "ZEC", "day_climb", 1.7895e9, real_close + 54000, 24.56, tid=32)
        self._recovery_file([self._t("ZEC", 1.7895e9, real_close, 9.03)])
        out = setup_ops.restore_lost_trades_once()
        assert out["corrected"] == 1 and out["restored"] == 0
        rows = db.query("SELECT ts_close, net_pnl_usd, attribution_json FROM trades WHERE symbol='ZEC'")
        assert len(rows) == 1, "the fabricated close must be replaced, not joined"
        assert abs(rows[0]["ts_close"] - real_close) < 1
        assert abs(rows[0]["net_pnl_usd"] - 9.03) < 1e-6
        assert "corrected" in (rows[0]["attribution_json"] or "")

    def test_a_trade_that_is_already_right_is_left_alone(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        _insert(db, "NEAR", "day_climb", 1.7896e9, 1.7896e9 + 3600, 3.90)
        before = db.query_one("SELECT id FROM trades WHERE symbol='NEAR'")["id"]
        self._recovery_file([self._t("NEAR", 1.7896e9, 1.7896e9 + 3600, 3.90)])
        out = setup_ops.restore_lost_trades_once()
        assert out["restored"] == 0 and out["corrected"] == 0
        assert db.query_one("SELECT id FROM trades WHERE symbol='NEAR'")["id"] == before

    def test_a_missing_recovery_file_is_not_an_error(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        out = setup_ops.restore_lost_trades_once()
        assert "error" not in out and out["restored"] == 0


class TestTheMarkKeepsUpWithReality:
    """A mark taken only at startup is stale the moment a trade closes, and a
    stale mark is exactly what lets the next restore reissue a used id. The gate
    caught this the first time it ran: mark 35, highest id 38."""

    def test_the_mark_is_never_left_below_the_highest_id(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        _insert(db, "A", "day_climb", 1.75e9, 1.75e9 + 1, 1.0)
        setup_ops.enforce_trade_id_high_water_mark()
        for sym in ("B", "C", "D"):                   # trades keep happening
            _insert(db, sym, "day_climb", 1.75e9, 1.75e9 + 1, 1.0)
        setup_ops.enforce_trade_id_high_water_mark()  # as the scheduler does
        mark = int(float(db.query_one(
            "SELECT value FROM app_state WHERE key=?",
            (setup_ops.ID_HIGH_WATER_KEY,))["value"]))
        top = db.query_one("SELECT MAX(id) m FROM trades")["m"]
        assert mark >= top, f"mark {mark} is below the highest id {top}"


class TestTheHoleIsDeclared:
    """A restored ledger is indistinguishable from a ledger where nothing happened.

    The operator went looking for a $1,132 ARB position he clearly remembered. It
    was real — its signal survives, and the next signal an hour later was rejected
    with 'risk block: ARB is already held' — but its order and position rows were
    in destroyed pages, and the restore came from before it opened. Nothing on any
    screen said a window was missing, so the absence read as a fact.

    It is declared, never re-created. A twenty-hour-old position closed at today's
    price is a fabricated trade; that is exactly how ZEC came to be recorded on the
    wrong day for three times its real profit.
    """

    def _gapfile(self, lost=("ARB pump_ride position, $1,132.13, never closed",)):
        import json as _json
        from app.config import get_settings
        p = Path(get_settings().db_file).parent / "data-gaps.json"
        p.write_text(_json.dumps({"gaps": [{
            "id": "corruption-2026-09-18", "from_ts": 1789649542.0, "to_ts": 1789715715.0,
            "from_label": "2026-09-17 05:52 CT", "to_label": "2026-09-18 00:15 CT",
            "cause": "destroyed by concurrent writes; restored from an older backup",
            "recovered": ["3 trades salvaged"], "known_lost": list(lost),
            "unrecoverable": True}]}))
        return p

    def test_the_gap_is_recorded_where_the_app_can_show_it(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        self._gapfile()
        assert setup_ops.declare_data_gaps_once()["declared"] == 1
        row = db.query_one("SELECT from_label, to_label, known_lost_json FROM data_gaps")
        assert row["from_label"] == "2026-09-17 05:52 CT"
        assert "1,132" in row["known_lost_json"]

    def test_declaring_twice_does_not_duplicate_the_gap(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        self._gapfile()
        setup_ops.declare_data_gaps_once()
        setup_ops.declare_data_gaps_once()
        assert db.query_one("SELECT COUNT(*) c FROM data_gaps")["c"] == 1

    def test_the_declaration_is_refreshed_not_frozen(self, writable_db):
        """The gap file is the source of truth — editing it must reach the table,
        or the declaration rots the way the scheduler intervals did."""
        from app.core import db, setup_ops
        db.init_db()
        self._gapfile()
        setup_ops.declare_data_gaps_once()
        self._gapfile(lost=("ARB pump_ride position", "and one more thing found later"))
        setup_ops.declare_data_gaps_once()
        assert "found later" in db.query_one("SELECT known_lost_json j FROM data_gaps")["j"]

    def test_the_invariant_puts_the_gap_on_screen(self, writable_db):
        from app.core import db, setup_ops
        from app.research import invariants
        db.init_db()
        self._gapfile()
        setup_ops.declare_data_gaps_once()
        r = invariants.known_gaps_are_declared()
        assert r["ok"] is True                    # a declared gap is not a defect
        assert "2026-09-17 05:52 CT" in r["detail"]
        assert r["n_total"] == 1

    def test_no_gap_file_is_not_an_error(self, writable_db):
        from app.core import db, setup_ops
        db.init_db()
        out = setup_ops.declare_data_gaps_once()
        assert "error" not in out and out["declared"] == 0

    def test_recovery_never_recreates_a_lost_position(self, writable_db):
        """The lost ARB position must NOT come back as an open position: closing a
        stale position at today's price is how a fabricated trade gets written."""
        from app.core import db, setup_ops
        db.init_db()
        self._gapfile()
        setup_ops.declare_data_gaps_once()
        assert db.query_one("SELECT COUNT(*) c FROM positions")["c"] == 0
        assert db.query_one("SELECT COUNT(*) c FROM trades WHERE symbol='ARB'")["c"] == 0
