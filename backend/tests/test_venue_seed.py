"""A seeded guess must not become permanent just because it was written first."""
import pytest

from app.core import db
from app.execution import venue_cost


@pytest.fixture(autouse=True)
def _db(writable_db):
    db.init_db()
    yield


def test_a_verified_seed_replaces_an_unverified_row():
    """kraken_pro sat at 26/40 bps for a fortnight with verified=0 and a source
    line saying it must be checked. When it was finally checked against
    Kraken's own schedule (40/80), INSERT OR IGNORE meant the correction could
    never reach the database."""
    venue_cost.ensure_schema()
    db.execute("""INSERT INTO venues(key,name,model,maker_bps,taker_bps,verified,
                                     source,note,supports_limit_orders,updated_at)
                  VALUES ('t_guess','T','fee',26.0,40.0,0,'guess','',1,1.0)
                  ON CONFLICT(key) DO UPDATE SET maker_bps=26.0, taker_bps=40.0, verified=0""")
    venue_cost.SEED_VENUES.append({
        "key": "t_guess", "name": "T", "model": "fee", "maker_bps": 40.0,
        "taker_bps": 80.0, "spread_pct": None, "verified": True,
        "source": "their own page", "note": "", "supports_limit_orders": True})
    try:
        venue_cost.ensure_schema()
        row = db.query_one("SELECT * FROM venues WHERE key='t_guess'")
        assert row["taker_bps"] == 80.0
        assert row["verified"] == 1
    finally:
        venue_cost.SEED_VENUES.pop()


def test_a_verified_row_is_never_overwritten_by_a_seed():
    """A figure corrected by hand from a real statement outranks source code."""
    venue_cost.ensure_schema()
    db.execute("UPDATE venues SET taker_bps=12.5, verified=1 WHERE key='kraken_pro'")
    venue_cost.ensure_schema()
    assert db.query_one("SELECT taker_bps FROM venues WHERE key='kraken_pro'")["taker_bps"] == 12.5


def test_kraken_pro_carries_krakens_own_tier_1_numbers():
    venue_cost.ensure_schema()
    row = db.query_one("SELECT * FROM venues WHERE key='kraken_pro'")
    assert row["maker_bps"] == 40.0, "Kraken Pro tier 1 maker is 0.40%"
    assert row["taker_bps"] == 80.0, "Kraken Pro tier 1 taker is 0.80%"
    assert row["verified"] == 1
