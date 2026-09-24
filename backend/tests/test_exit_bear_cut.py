"""bear_cut_* exits when the coin's OWN day has the bear shape -- below its
open, lower highs, lower lows -- and does nothing on a day that climbs.

The ZEC observation (2026-09-19): "primary trend is down ... it keeps going
down, so is it better to cut the losses sooner". A rule, so the replay can say.
"""
import datetime as dt
import time

from app.core import db
from app.research import exit_lab


def _seed(writable_db, shape: str):
    tz = exit_lab.TZ
    day0 = dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=2)
    t_entry = (day0 + dt.timedelta(hours=8)).timestamp()
    rows = []
    for i in range(0, 24 * 4):              # the whole day in 15-minute bars
        ts = day0.timestamp() + i * 900
        hrs = i / 4.0
        if shape == "bear":
            px = 100.0 - 1.2 * hrs          # steady decline all day
        else:
            px = 100.0 + 1.2 * hrs          # steady climb all day
        rows.append(("ZZZ", 900, ts, px, px * 1.002, px * 0.998, px, 1000.0, "test"))
    db.executemany("INSERT INTO bars(symbol, granularity, ts, open, high, low, close, volume, source) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    entry_px = 100.0 - 1.2 * 8 if shape == "bear" else 100.0 + 1.2 * 8
    db.execute("INSERT INTO trades(symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close, "
               "holding_s, gross_pnl_usd, cost_usd, net_pnl_usd) "
               "VALUES ('ZZZ','day_climb','paper',1,?,?,?,?,3600,0,0,0)",
               (entry_px, entry_px, t_entry, t_entry + 3600))
    return db.query_one("SELECT * FROM trades ORDER BY id DESC LIMIT 1")


def test_bear_day_is_cut_early_and_loses_less_than_the_trail(writable_db):
    t = _seed(writable_db, "bear")
    res = {r["rule"]: r for r in exit_lab.replay(dict(t))}
    assert res["bear_cut_3h"]["hours_held"] < res["trail_8_no_floor"]["hours_held"]
    assert res["bear_cut_3h"]["net_pct"] > res["trail_8_no_floor"]["net_pct"]


def test_a_climbing_day_is_left_alone(writable_db):
    t = _seed(writable_db, "climb")
    res = {r["rule"]: r for r in exit_lab.replay(dict(t))}
    assert res["bear_cut_3h"]["net_pct"] == res["trail_8_no_floor"]["net_pct"]
    assert res["bear_cut_3h"]["hours_held"] == res["trail_8_no_floor"]["hours_held"]
