"""Which exit rule actually makes money on a pump entry?

The operator's objection, and it is correct:

    "if you dont have exit strategy and just wait for the process to drop below
     the cost then what is point of getting in if no matter what we are just
     waiting for price to drop to exit!"

A pure trailing stop exits on a decline BY CONSTRUCTION. It gives back the trail
distance from the peak every single time, plus the round trip. On the live ARB
position that meant the price had to rise 9.74% from entry before the exit could
even be green.

So: same entries, seven different exits, four years of hourly bars, split
chronologically. The first 75% chooses; the last 25% is read once at the end.
"""
import sqlite3, numpy as np, json, sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "tradecrypto.sqlite"
ROUND_TRIP = 0.019182          # (1+s)/(1-s)-1 at 0.95% a side
SIDE = 0.0095
PUMP_BARS, PUMP_PCT = 4, 18.0  # pump_ride's entry, unchanged
MAX_HOLD = 24

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
syms = [r[0] for r in con.execute(
    "SELECT symbol FROM bars WHERE granularity=3600 GROUP BY symbol HAVING COUNT(*)>2000")]

def series(sym):
    rows = con.execute("SELECT ts,open,high,low,close FROM bars WHERE symbol=? AND granularity=3600 "
                       "AND close IS NOT NULL ORDER BY ts", (sym,)).fetchall()
    a = np.array(rows, dtype=float)
    return a[:,0], a[:,1], a[:,2], a[:,3], a[:,4]

def entries(c):
    """pump_ride: 4-bar move >= 18%, no re-entry while a trade is open."""
    out, t = [], PUMP_BARS
    while t < len(c) - 2:
        if c[t-PUMP_BARS] > 0 and (c[t]/c[t-PUMP_BARS]-1)*100 >= PUMP_PCT:
            out.append(t+1)              # fill on the NEXT bar, as the engine does
            t += MAX_HOLD
        else:
            t += 1
    return out

# ── the exit rules ──────────────────────────────────────────────────────────
def run_exit(o,h,l,c,e,rule):
    """Returns net return after both spreads. e = entry bar index."""
    entry = c[e]*(1+SIDE)                      # buy side paid here
    peak = c[e]
    be = entry/(1-SIDE)                        # price where a sale breaks even
    for k in range(e+1, min(e+1+MAX_HOLD, len(c))):
        peak = max(peak, h[k])
        age = k-e
        stop = rule(entry, peak, be, age, c, e, k, l, h)
        if stop is not None and l[k] <= stop:
            return stop*(1-SIDE)/entry - 1
        tgt = rule.target(entry, peak, be, age) if hasattr(rule,'target') else None
        if tgt is not None and h[k] >= tgt:
            return tgt*(1-SIDE)/entry - 1
    k = min(e+MAX_HOLD, len(c)-1)
    return c[k]*(1-SIDE)/entry - 1

def mk(trail=None, breakeven=False, decay=None, atr_k=None, target=None, hard=0.10):
    def rule(entry, peak, be, age, c, e, k, l, h):
        t = trail
        if decay:  t = max(decay[1], trail - decay[0]*age)
        if atr_k is not None:
            w = slice(max(0,e-24), e+1)
            atr = float(np.nanmean(h[w]-l[w])/max(c[e],1e-12))
            t = min(0.25, max(0.03, atr_k*atr))
        s = peak*(1-t) if t else entry*(1-hard)
        if breakeven and peak >= be*(1+0.005):
            s = max(s, be)                      # never give back a covered trade
        return max(s, entry*(1-hard))
    if target is not None:
        rule.target = lambda entry, peak, be, age, tg=target: entry*(1+tg)
    return rule

RULES = {
 "A trail 8% (current)":      mk(trail=0.08),
 "B trail 8% + breakeven":    mk(trail=0.08, breakeven=True),
 "C trail 12% + breakeven":   mk(trail=0.12, breakeven=True),
 "D trail 5% + breakeven":    mk(trail=0.05, breakeven=True),
 "E decay 15%->5% + be":      mk(trail=0.15, decay=(0.01,0.05), breakeven=True),
 "F vol-scaled trail + be":   mk(trail=0.08, atr_k=3.0, breakeven=True),
 "G target +8%, stop -10%":   mk(trail=None, target=0.08),
}

rows = []
for i, s in enumerate(syms):
    ts,o,h,l,c = series(s)
    for e in entries(c):
        if e+2 >= len(c): continue
        r = {"sym": s, "ts": float(ts[e])}
        for name, rule in RULES.items():
            r[name] = run_exit(o,h,l,c,e,rule)
        rows.append(r)
OUT = Path(__file__).resolve().parent / "exit_rule_study_rows.json"
json.dump(rows, open(OUT, "w"))
print(f"{len(rows)} pump entries across {len(syms)} coins")
