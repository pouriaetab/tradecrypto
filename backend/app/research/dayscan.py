"""One row per coin per day: what moved, when, and how loud it was.

Built for the question the operator actually asks every morning -- "which of the coins
I can trade are having their day?" -- and for stepping back through past days to
see whether the tells were there before the move.

Everything is computed from the `bars` table on demand. No new storage, so it
works on history we already have (hourly bars back to 2022) and stays correct as
new bars land. Days are Austin local, midnight to midnight.

Definitions are fixed and deliberate -- these exact words are shown in the UI:
  move        open of the day  ->  last price we have for that day
  swing       day's low        ->  day's high      (total distance travelled)
  open_high   day's open       ->  day's high      (best exit that was available)
  off_high    last price       ->  day's high      (how much it gave back)
  vol_vs_7d   the day's volume against its own average day over the prior 7 days
  vol_peak    the clock hour holding the most volume, and that hour's share
  swing_peak  the clock hour with the widest high-to-low, and how wide
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import db

TZ = ZoneInfo("America/Chicago")
MAX_PATH_POINTS = 150     # a 1-minute day is 1440 bars; nobody can see more than ~150
_CACHE: dict = {}         # (day, as_of, hot_v, hot_s, tradable) -> (expires_at, payload)
_CACHE_MAX = 400
_INDEXED = False


def _ensure_index() -> None:
    """One-off index so a day-range scan does not walk the whole bars table.

    The primary key starts with `symbol`, so filtering on granularity+ts alone
    could not use it and every date change re-scanned millions of rows. This is
    why changing dates felt slow.
    """
    global _INDEXED
    if _INDEXED:
        return
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_bars_gran_ts ON bars(granularity, ts)")
        _INDEXED = True
    except Exception:
        _INDEXED = True   # never let this break a scan


def _downsample(rows: list, op: float) -> tuple[list, list, list]:
    """Bucket a day into at most MAX_PATH_POINTS points: last close, summed volume."""
    n = len(rows)
    if n <= MAX_PATH_POINTS:
        picked = [(r, r.get("volume") or 0.0) for r in rows]
    else:
        step = n / MAX_PATH_POINTS
        picked = []
        for i in range(MAX_PATH_POINTS):
            a, b = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
            chunk = rows[a:min(b, n)]
            if chunk:
                picked.append((chunk[-1], sum((c.get("volume") or 0.0) for c in chunk)))
    path = [round((r["close"] / op - 1) * 100, 3) for r, _ in picked]
    vols = [round(v, 4) for _, v in picked]
    times = [datetime.fromtimestamp(r["ts"], TZ).strftime("%H:%M") for r, _ in picked]
    return path, vols, times
# Defaults chosen from a sweep over June 2024 -> Sep 2026, 823 days, 33 tradable coins,
# scoring "was the day's best coin among the ones we flagged?" at 04:00:
#   vol >= 1.5x -> 8.7 coins/day, best-of-day caught 44.3% of days, picks avg +4.12% room left
#   vol >= 2.0x -> 5.5 coins/day, 33.3%, +4.49%
#   vol >= 3.0x -> 2.8 coins/day, 20.8%, +4.92%
# A wider net catches the winner more often; a tighter one gives each pick more room.
# 1.5x is the default because missing the day's mover costs more than a few extra rows.
HOT_VOLUME = 1.5
HOT_SWING = 1.0
ROUND_TRIP_PCT = 1.92 # Robinhood measured spread, both sides


def _day_bounds(d: date) -> tuple[int, int]:
    start = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def _rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 2:
        return None
    au = ad = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        up, dn = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            au += up / n
            ad += dn / n
        else:
            au = (au * (n - 1) + up) / n
            ad = (ad * (n - 1) + dn) / n
    if ad <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def _tradable() -> set[str]:
    """Coins the ENGINE will actually trade right now.

    This used to read rh_spreads, which is a log of measured spreads and not a
    tradability list at all. It happened to hold 33 rows, so every scan and every
    backtest silently ran on 33 coins and nobody said so.
    """
    return {r["symbol"] for r in db.query(
        "SELECT symbol FROM universe WHERE role IN ('core','tradeable') AND active=1")}


def _status_map() -> dict[str, str]:
    """Per-coin trading status, so the scan can show everything and still say
    which ones are actually reachable. Sorting beats hiding."""
    out: dict[str, str] = {}
    for r in db.query("SELECT symbol, role, rh_confirmed FROM universe WHERE active=1"):
        if r["role"] in ("core", "tradeable"):
            out[r["symbol"]] = "tradeable"
        elif r["rh_confirmed"]:
            out[r["symbol"]] = "on RH, not enabled"
        else:
            out[r["symbol"]] = "not on RH"
    return out


def _rh_confirmed() -> set[str]:
    """Coins ROBINHOOD will fill -- `universe.rh_confirmed`, set only once the RH
    MCP listed the pair as USD and is_api_tradable.

    Deliberately separate from `_tradable()`. That one is OUR gate (`universe.role`
    is core / tradeable / watch / excluded) and answers "is the engine allowed to
    trade it". This one answers "will the venue take the order at all". A coin can
    be on Robinhood and still be role='watch', so the two counts differ and must
    never be reported as one number.
    """
    return {r["symbol"] for r in db.query(
        "SELECT symbol FROM universe WHERE rh_confirmed=1 AND active=1")}


def available_days(limit: int = 900) -> list[str]:
    """Which days we can actually draw, newest first."""
    rows = db.query(
        # Was GROUP BY ts/86400, which buckets by UTC day. MIN(ts) of a UTC day is
        # 19:00 the PREVIOUS Chicago day, so every label came back one day early
        # and today never appeared in the list until 19:00 local. Bucket by the
        # local date directly.
        # day-boundary-ok: lists which days have bars to scan, not money; the scan reads each day's own bars by ts afterwards
        "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') AS d, MAX(ts) AS t "
        "FROM bars WHERE granularity=3600 GROUP BY d ORDER BY d DESC LIMIT ?",
        (limit + 2,))
    seen, out = set(), []
    for r in rows:
        d = r["d"]
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out[:limit]


def scan_day(day: str | None = None, tradable_only: bool = True,
             as_of_hour: int | None = None, hot_volume: float | None = None,
             hot_swing: float | None = None) -> dict:
    """Cached wrapper. A finished day never changes, so it is cached for good;
    today is cached briefly so repeated polls stay cheap."""
    _ensure_index()
    today = datetime.now(TZ).date().isoformat()
    d_key = day or today
    key = (d_key, as_of_hour, hot_volume, hot_swing, tradable_only)
    hit = _CACHE.get(key)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    out = _scan_day_uncached(day, tradable_only, as_of_hour, hot_volume, hot_swing)
    ttl = 45.0 if (d_key == today and as_of_hour is None) else 86400.0 * 30
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now + ttl, out)
    return out


def _scan_day_uncached(day: str | None = None, tradable_only: bool = True,
                       as_of_hour: int | None = None, hot_volume: float | None = None,
                       hot_swing: float | None = None) -> dict:
    """The table for one day.

    `as_of_hour` replays the day as it looked at that hour -- nothing after it is
    used. That is what makes a past day honest to look at: at 06:00 you only get
    what 06:00 knew.
    """
    d = date.fromisoformat(day) if day else datetime.now(TZ).date()
    t0, t1 = _day_bounds(d)
    if as_of_hour is not None:
        t1 = min(t1, t0 + int(as_of_hour) * 3600)
    hist0 = t0 - 31 * 86400

    keep = _tradable() if tradable_only else None
    status = _status_map()
    rh_ok = _rh_confirmed()

    # Finest granularity PER COIN, not one granularity for the whole scan.
    #
    # This picked a single granularity by counting BARS: if any coin had minute
    # bars for the day, gran became 60 for everyone -- and minute bars are only
    # backfilled for a 25-coin subset. So the scan silently showed 25 of 75
    # coins, and the operator was told "all tradable coins" while looking at a
    # third of them. Resolution per coin is the right axis: each coin appears at
    # the best data it has, and no coin disappears for lacking minute bars.
    gran_counts = {}
    for g in (60, 900, 3600):
        for r in db.query(
                "SELECT symbol, COUNT(*) c FROM bars WHERE granularity=? AND ts>=? AND ts<? "
                "GROUP BY symbol", (g, t0, t1)):
            if r["c"] >= 4:
                gran_counts.setdefault(r["symbol"], g)   # (60, 900, 3600) -> finest wins
    gran = min(gran_counts.values()) if gran_counts else 3600

    intraday = []
    for g in sorted(set(gran_counts.values())):
        syms_g = [sy for sy, gg in gran_counts.items() if gg == g]
        if not syms_g:
            continue
        place = ",".join("?" * len(syms_g))
        intraday.extend(db.query(
            f"SELECT symbol, ts, open, high, low, close, volume FROM bars "
            f"WHERE granularity=? AND ts>=? AND ts<? AND symbol IN ({place}) "
            f"ORDER BY symbol, ts", (g, t0, t1, *syms_g)))
    hist = db.query(
        "SELECT symbol, ts, open, high, low, close, volume FROM bars "
        "WHERE granularity=3600 AND ts>=? AND ts<? ORDER BY symbol, ts", (hist0, t0))

    by_sym: dict[str, list] = {}
    for r in intraday:
        if keep is not None and r["symbol"] not in keep:
            continue
        by_sym.setdefault(r["symbol"], []).append(r)
    hist_sym: dict[str, list] = {}
    for r in hist:
        hist_sym.setdefault(r["symbol"], []).append(r)

    coins = []
    for sym, rows in by_sym.items():
        rows = [r for r in rows if r["open"] and r["close"]]
        if len(rows) < 4:
            continue
        op = rows[0]["open"]
        last = rows[-1]["close"]
        hi = max(r["high"] for r in rows if r["high"])
        lo = min(r["low"] for r in rows if r["low"])
        if not (op and hi and lo and last):
            continue
        vol = sum((r["volume"] or 0.0) for r in rows)

        # per clock hour, for the two peak columns
        hours: dict[int, dict] = {}
        for r in rows:
            h = datetime.fromtimestamp(r["ts"], TZ).hour
            b = hours.setdefault(h, {"v": 0.0, "hi": None, "lo": None})
            b["v"] += r["volume"] or 0.0
            if r["high"]:
                b["hi"] = r["high"] if b["hi"] is None else max(b["hi"], r["high"])
            if r["low"]:
                b["lo"] = r["low"] if b["lo"] is None else min(b["lo"], r["low"])
        vol_peak = max(hours.items(), key=lambda kv: kv[1]["v"]) if hours else None
        swings = {h: (b["hi"] / b["lo"] - 1) * 100
                  for h, b in hours.items() if b["hi"] and b["lo"] and b["lo"] > 0}
        swing_peak = max(swings.items(), key=lambda kv: kv[1]) if swings else None

        # baselines from prior complete days
        hrows = hist_sym.get(sym, [])
        per_day: dict[str, dict] = {}
        for r in hrows:
            k = datetime.fromtimestamp(r["ts"], TZ).date().isoformat()
            b = per_day.setdefault(k, {"v": 0.0, "hi": None, "lo": None})
            b["v"] += r["volume"] or 0.0
            if r["high"]:
                b["hi"] = r["high"] if b["hi"] is None else max(b["hi"], r["high"])
            if r["low"]:
                b["lo"] = r["low"] if b["lo"] is None else min(b["lo"], r["low"])
        days_sorted = [per_day[k] for k in sorted(per_day)]
        v7 = [b["v"] for b in days_sorted[-7:] if b["v"] > 0]
        v30 = [b["v"] for b in days_sorted[-30:] if b["v"] > 0]
        r7 = [(b["hi"] / b["lo"] - 1) * 100 for b in days_sorted[-7:]
              if b["hi"] and b["lo"] and b["lo"] > 0]

        elapsed_h = max(1.0, (rows[-1]["ts"] - t0) / 3600.0 + 1)
        pace = vol / (elapsed_h / 24.0)          # volume projected to a full day
        swing_pct = (hi / lo - 1) * 100

        closes = [r["close"] for r in hrows[-260:] if r["close"]]
        _path, _vols, _times = _downsample(rows, op)

        coins.append({
            "symbol": sym,
            "status": status.get(sym, "unknown"),
            # Two different questions, kept apart on purpose: `tradable` is our own
            # gate (universe.role), `rh_tradable` is whether Robinhood will fill it
            # at all (universe.rh_confirmed). Never the same count.
            "tradable": status.get(sym) == "tradeable",
            "rh_tradable": sym in rh_ok,
            "price": last,
            "open": op, "high": hi, "low": lo,
            "move": (last / op - 1) * 100,
            "swing": swing_pct,
            "open_high": (hi / op - 1) * 100,
            "off_high": (last / hi - 1) * 100,
            "volume": vol,
            "volume_usd": vol * last,
            "vol_vs_7d": (pace / (sum(v7) / len(v7))) if v7 else None,
            "vol_vs_30d": (pace / (sum(v30) / len(v30))) if v30 else None,
            "swing_vs_7d": (swing_pct / (sum(r7) / len(r7))) if r7 else None,
            "vol_peak_hour": f"{vol_peak[0]:02d}:00" if vol_peak else None,
            "vol_peak_share": (vol_peak[1]["v"] / vol * 100) if vol_peak and vol else None,
            "swing_peak_hour": f"{swing_peak[0]:02d}:00" if swing_peak else None,
            "swing_peak_pct": swing_peak[1] if swing_peak else None,
            "rsi": _rsi(closes),
            "hours_elapsed": round(elapsed_h, 1),
            "path": _path, "path_volume": _vols, "path_times": _times,
            "path_low": min(r["low"] for r in rows if r["low"]),
            "path_high": hi,
        })

    hv = HOT_VOLUME if hot_volume is None else float(hot_volume)
    hs = HOT_SWING if hot_swing is None else float(hot_swing)
    for c in coins:
        c["in_play"] = bool((c["vol_vs_7d"] or 0) >= hv and (c["swing_vs_7d"] or 0) >= hs)
    coins.sort(key=lambda c: -c["move"])

    # WHY THESE COUNTS EXIST
    # The coin count on this page has read 33, then 58, then 25, then 75, and the
    # operator reasonably stopped believing any of them. Each number was a
    # different question being answered silently: 33 was rh_spreads (a log of
    # measured spreads, never a tradability list) mistaken for the universe, 58 is
    # how many coins Robinhood will fill, 25 was the minute-bar subset leaking in
    # through a single shared granularity, and 75 is every active coin with bars
    # today -- the correct figure. So the payload now states every number at once,
    # side by side, each computed from the rows actually returned below. The header
    # renders these verbatim and hardcodes nothing.
    #
    # The distinction that caused the confusion, in one line:
    #   rh_tradable     Robinhood will fill it        universe.rh_confirmed
    #   engine_enabled  we allow the engine to trade  universe.role in (core, tradeable)
    by_status: dict[str, int] = {}
    for row in coins:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    counts = {
        "scanned": len(coins),                                                  # = len(coins), always
        "rh_tradable": sum(1 for c in coins if c["rh_tradable"]),               # venue says yes
        "not_rh_tradable": sum(1 for c in coins if not c["rh_tradable"]),       # venue says no
        "engine_enabled": sum(1 for c in coins if c["tradable"]),               # our role gate says yes
        "rh_not_enabled": sum(1 for c in coins                                  # venue yes, our gate no
                              if c["rh_tradable"] and not c["tradable"]),
        "universe_active": len(status),          # active coins we track, bars or not
        "no_bars_today": max(0, len(status) - len(coins)),   # tracked but nothing to draw
        "by_status": by_status,                  # exact status strings, for the filter buttons
    }

    return {
        "date": d.isoformat(),
        "as_of_hour": as_of_hour,
        "granularity_s": gran,
        "tradable_only": tradable_only,
        "count": len(coins),
        "counts": counts,
        "in_play": sum(1 for c in coins if c["in_play"]),
        "up": sum(1 for c in coins if c["move"] > 0),
        "round_trip_pct": ROUND_TRIP_PCT,
        "coins": coins,
        "thresholds": {"hot_volume": hv, "hot_swing": hs},
    }


def scan_range(start: str, end: str, tradable_only: bool = True,
                hot_volume: float | None = None) -> dict:
    """One summary line per day, for the drawer's day list."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    days, cur = [], d0
    while cur <= d1 and len(days) < 400:
        s = scan_day(cur.isoformat(), tradable_only=tradable_only, hot_volume=hot_volume)
        movers = [c for c in s["coins"] if c["in_play"]]
        best = max(s["coins"], key=lambda c: c["open_high"], default=None)
        days.append({
            "date": s["date"],
            "count": s["count"],
            "in_play": s["in_play"],
            "up": s["up"],
            "movers": [c["symbol"] for c in sorted(movers, key=lambda c: -c["move"])[:6]],
            "best_symbol": best["symbol"] if best else None,
            "best_open_high": best["open_high"] if best else None,
            "median_move": (sorted(c["move"] for c in s["coins"])[len(s["coins"]) // 2]
                            if s["coins"] else None),
        })
        cur += timedelta(days=1)
    days.sort(key=lambda x: x["date"], reverse=True)
    return {"start": d0.isoformat(), "end": d1.isoformat(), "days": days}
