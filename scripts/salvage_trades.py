"""Pull `trades` rows straight out of the raw pages of a headerless SQLite file.

The 2026-09-18 corruption destroyed the file header and the page-1 schema, so
SQLite itself will not open the file and `.recover` has nothing to start from.
But the B-tree leaf pages are still there, and a row is self-describing: a
record header listing serial types, then the values. We know exactly what a
trades row looks like, so we can find them without a schema.

The shape we match (18 columns, from the live database):
    id, symbol, strategy, mode, qty, entry_px, exit_px, ts_open, ts_close,
    holding_s, gross_pnl_usd, cost_usd, net_pnl_usd, predicted_edge_bps,
    realised_edge_bps, attribution_json, open_order_id, close_order_id
"""
import sys, json, datetime as dt, zoneinfo

CT = zoneinfo.ZoneInfo("America/Chicago")
STRATS = {"volume_build", "day_climb", "morning_dip", "oversold_turn", "pump_ride"}
MODES = {"paper", "live", "advisory", "mcp"}


def varint(buf, i):
    n = 0
    for k in range(9):
        if i >= len(buf): raise IndexError
        b = buf[i]; i += 1
        if k == 8: return (n << 8) | b, i
        n = (n << 7) | (b & 0x7F)
        if not (b & 0x80): return n, i
    return n, i


def serial_len(t):
    if t == 0 or t == 8 or t == 9: return 0
    if t <= 4: return t
    if t == 5: return 6
    if t == 6 or t == 7: return 8
    return (t - 12) // 2 if t % 2 == 0 else (t - 13) // 2


def decode(buf, t):
    if t == 0: return None
    if t == 8: return 0
    if t == 9: return 1
    if t <= 6:
        n = {1:1,2:2,3:3,4:4,5:6,6:8}[t]
        return int.from_bytes(buf[:n], "big", signed=True)
    if t == 7:
        import struct; return struct.unpack(">d", buf[:8])[0]
    if t % 2 == 0: return buf                       # blob
    return buf.decode("utf-8", "replace")           # text


def records(page):
    """Every complete record in a table-leaf page."""
    if not page or page[0] != 0x0D: return
    ncells = int.from_bytes(page[3:5], "big")
    if not (0 < ncells < 1000): return
    for k in range(ncells):
        po = 8 + k * 2
        if po + 2 > len(page): return
        off = int.from_bytes(page[po:po+2], "big")
        if not (0 < off < len(page)): continue
        try:
            plen, i = varint(page, off)
            rowid, i = varint(page, i)
            if plen <= 0 or i + plen > len(page): continue   # overflow: skip
            body = page[i:i+plen]
            hlen, j = varint(body, 0)
            types = []
            while j < hlen:
                t, j = varint(body, j); types.append(t)
            vals, p = [], hlen
            for t in types:
                n = serial_len(t)
                vals.append(decode(body[p:p+n], t)); p += n
            yield rowid, vals
        except (IndexError, ValueError, UnicodeDecodeError):
            continue


def looks_like_trade(v):
    return (len(v) >= 15
            and isinstance(v[2], str) and v[2] in STRATS
            and isinstance(v[3], str) and v[3] in MODES
            and isinstance(v[1], str) and 1 <= len(v[1]) <= 12
            and isinstance(v[7], (int, float)) and 1.7e9 < float(v[7]) < 2.1e9)


def main(path, page_size=4096):
    out, seen = [], set()
    with open(path, "rb") as fh:
        pageno = 0
        while True:
            page = fh.read(page_size)
            if len(page) < page_size: break
            pageno += 1
            for rowid, v in records(page):
                if looks_like_trade(v):
                    key = (v[1], v[2], round(float(v[7]), 3))
                    if key in seen: continue
                    seen.add(key)
                    out.append({"id": v[0] if v[0] is not None else rowid,
                                "symbol": v[1], "strategy": v[2],
                                "mode": v[3], "qty": v[4], "entry_px": v[5],
                                "exit_px": v[6], "ts_open": v[7], "ts_close": v[8],
                                "holding_s": v[9], "gross": v[10], "cost": v[11],
                                "net": v[12], "page": pageno})
    out.sort(key=lambda r: (r["ts_open"] or 0))
    return out


if __name__ == "__main__":
    rows = main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 4096)
    print(f"recovered {len(rows)} trade row(s)\n")
    for r in rows:
        o = dt.datetime.fromtimestamp(float(r["ts_open"]), CT)
        c = (dt.datetime.fromtimestamp(float(r["ts_close"]), CT).strftime("%m-%d %H:%M")
             if r["ts_close"] else "open")
        net = r["net"] if isinstance(r["net"], (int, float)) else 0.0
        rid = r["id"] if r["id"] is not None else "?"
        print(f"  #{str(rid):<4} {r['symbol']:<7} {r['strategy']:<13} "
              f"opened {o:%Y-%m-%d %H:%M}  closed {c:<12} net {net:+.2f}")
    json.dump(rows, open("recovered_trades.json", "w"), indent=1, default=str)
    print("\n-> recovered_trades.json")
