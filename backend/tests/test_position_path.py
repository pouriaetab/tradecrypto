"""How far up and how far down an open position has been, net of BOTH sides.

The Risk tab could only ever say what a position is worth now. It could not say
that WLD was $4 up two hours ago and is $1 down now -- and that difference is
the whole argument about exits. Asked for 2026-09-22.
"""
import time

import pytest

from app.core import db
from app.research import position_path


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def _bar(symbol, ts, high, low, close, g=900):
    db.execute(
        "INSERT OR REPLACE INTO bars(symbol, ts, granularity, open, high, low, close, volume, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol, int(ts), g, close, high, low, close, 1.0, "test"))


def _pos(symbol="C", qty=100.0, entry=1.0, opened=None):
    return {"symbol": symbol, "strategy": "s", "qty": qty, "avg_px": entry,
            "opened_ts": opened if opened is not None else time.time() - 3600}


def test_both_sides_of_the_spread_are_charged(monkeypatch):
    """Entry price already carries the BUY spread; the mark must charge the SELL
    spread. A position whose price has not moved is DOWN by one side, not flat."""
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.01, True))
    t = time.time() - 3600
    for i in range(4):
        _bar("C", t + i * 900, high=1.0, low=1.0, close=1.0)
    out = position_path.for_position(_pos(opened=t - 1))
    assert out["peak_usd"] == pytest.approx(-1.0)   # 100 units x $1 x 1%
    assert out["trough_usd"] == pytest.approx(-1.0)


def test_peak_uses_the_high_and_trough_uses_the_low(monkeypatch):
    """A close-only walk quietly reports a smaller range than really happened."""
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 4 * 3600
    _bar("C", t + 900, high=1.20, low=0.95, close=1.00)
    _bar("C", t + 1800, high=1.05, low=0.80, close=1.00)
    out = position_path.for_position(_pos(opened=t))
    assert out["peak_usd"] == pytest.approx(20.0)     # 1.20 high
    assert out["trough_usd"] == pytest.approx(-20.0)  # 0.80 low


def test_the_times_are_reported_and_are_relative_to_entry(monkeypatch):
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 4 * 3600
    _bar("C", t + 900, high=1.01, low=0.99, close=1.0)
    _bar("C", t + 3600, high=1.50, low=0.99, close=1.0)      # the peak, an hour in
    _bar("C", t + 5400, high=1.01, low=0.50, close=1.0)      # the trough, 90 min in
    out = position_path.for_position(_pos(opened=t))
    assert out["peak_after_s"] == pytest.approx(3600, abs=1)
    assert out["trough_after_s"] == pytest.approx(5400, abs=1)
    assert out["peak_ts"] > out["opened_ts"]


def test_a_high_printed_before_entry_is_not_credited(monkeypatch):
    """Buying after the spike must not inherit the spike."""
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 4 * 3600
    _bar("C", t, high=9.00, low=1.00, close=1.0)            # BEFORE the entry
    _bar("C", t + 900, high=1.10, low=1.00, close=1.0)
    _bar("C", t + 1800, high=1.10, low=1.00, close=1.0)
    out = position_path.for_position(_pos(opened=t + 500))
    assert out["peak_usd"] == pytest.approx(10.0), "a pre-entry high was counted"


def test_given_back_is_the_peak_minus_now(monkeypatch):
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 4 * 3600
    _bar("C", t + 900, high=1.50, low=1.00, close=1.50)
    _bar("C", t + 1800, high=1.50, low=1.00, close=1.10)
    out = position_path.for_position(_pos(opened=t))
    assert out["now_usd"] == pytest.approx(10.0)
    assert out["given_back_usd"] == pytest.approx(40.0)


def test_no_bars_says_so_rather_than_inventing_a_range(monkeypatch):
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    out = position_path.for_position(_pos(symbol="NOBARS"))
    assert out["peak_usd"] is None and out["trough_usd"] is None
    assert out["why"] and "cannot be reconstructed" in out["why"]


def test_the_resolution_is_reported_not_implied(monkeypatch):
    """The timestamp is the BAR the extreme fell in, not the minute."""
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 4 * 3600
    for i in range(4):
        _bar("C", t + i * 900, high=1.1, low=0.9, close=1.0, g=900)
    out = position_path.for_position(_pos(opened=t))
    assert out["granularity_s"] == 900
    assert out["bars"] == 4


def test_it_uses_the_finest_granularity_available(monkeypatch):
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    t = time.time() - 8 * 3600
    for i in range(3):
        _bar("C", t + i * 3600, high=2.0, low=0.5, close=1.0, g=3600)
    for i in range(6):
        _bar("C", t + i * 900, high=1.2, low=0.8, close=1.0, g=900)
    out = position_path.for_position(_pos(opened=t))
    assert out["granularity_s"] == 900, "fell back to hourly bars when 15-minute ones exist"


def test_a_broken_position_does_not_take_the_whole_table_down(monkeypatch):
    from app.risk import guards
    monkeypatch.setattr(guards, "open_positions", lambda m: [
        _pos(symbol="GOOD"), {"symbol": "BAD", "strategy": "s"}])
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    out = position_path.open_paths("paper")
    assert len(out["positions"]) == 2
    assert any(r.get("why") for r in out["positions"])


def test_the_endpoint_is_wired():
    import inspect
    from app.api.v1 import routes
    src = inspect.getsource(routes)
    assert "/positions/path" in src
    assert "position_path" in src


def test_a_missing_bars_table_does_not_blank_the_row(monkeypatch):
    """A trimmed database has no bars at all. The peak and trough are then
    unknowable -- the size, the age and the current mark are not, and losing
    them turns one missing column into an empty table."""
    from app.core import db as _db
    real = _db.query

    def no_bars(sql, params=()):
        if "FROM bars" in sql:
            raise Exception("no such table: bars")
        return real(sql, params)

    monkeypatch.setattr(_db, "query", no_bars)
    monkeypatch.setattr(position_path, "_spread_frac", lambda s: (0.0, True))
    out = position_path.for_position(_pos(opened=time.time() - 7200))
    assert out["cost_basis_usd"] == pytest.approx(100.0)
    assert out["age_s"] == pytest.approx(7200, abs=5)
    assert out["peak_usd"] is None
    assert out["why"]
