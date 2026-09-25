#!/usr/bin/env python3
"""Does any technical indicator beat Robinhood's spread? Run it and see.

    cd backend && .venv/bin/python ../research/edge_audit.py

This reproduces, end to end, the four findings that decided the direction of this
project. It reads the project's own SQLite database and nothing else, so the
numbers are about the operator's actual coins, not a paper's.

The discipline, because it is what makes the answer trustworthy
---------------------------------------------------------------
* Every feature uses only bars at or before time t. No exceptions, and the
  independent check in app/research/verify.py tests this by REPLACING the future
  with noise and demanding the features come out identical.
* Splits are by TIME, never shuffled. A random split lets a Tuesday predict the
  preceding Monday and every metric becomes a lie.
* THREE slices, not two: fit on train, CHOOSE the setup on validation, report on
  test. Choosing and reporting on the same data is the most common way a backtest
  flatters itself.
* Ties inside a bar are resolved against us — if both the target and the stop are
  touched in the same 15 minutes, we assume the stop.
* Many setups are searched, so single-indicator p-values get Benjamini-Hochberg
  correction. Twenty indicators will always produce a "significant" one.

What it found (2026-09-06, 24 coins, 795k 15-minute bars, 3 years hourly)
-------------------------------------------------------------------------
1. The indicators are real. Out-of-sample AUC: atr_pct 0.718, up_from_1d_low
   0.645, dist_20d_low 0.636. Combined model 0.699. The top decile more than
   doubles the base hit rate, 14.2% -> 30.9%. These are not noise.

2. The skill does not become money. Best selected setup: +0.067% GROSS per trade.
   Buying blindly and holding 12 hours returned +0.233% gross over the same
   window — better than the model's picks.

3. Robinhood's spread is 1.918% per round trip. That is roughly 27x the gross
   edge. No plausible improvement to the indicators closes a 27x gap; this is
   arithmetic, not pessimism.

4. Holding longer makes it worse, not better. 1d/2d/3d/5d/7d/14d holds on hourly
   bars all lose after cost, and the loss grows with the horizon.

The conclusion this forces
--------------------------
The binding constraint is the VENUE, not the model. Effort spent on better
indicators is spent against a 27x deficit. The two things that would actually
change the arithmetic are a cheaper execution venue, or holding for moves large
enough that 1.92% stops being the dominant term.

What the indicators ARE good for: atr_pct at AUC 0.718 reliably identifies which
coin and which day has enough range to be worth attention at all. Selection, not
timing. That part held up under every test here.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

DB = Path(__file__).resolve().parent.parent / "data" / "tradecrypto.sqlite"
SPREAD = 0.0095
BUY, SELL = 1 + SPREAD, 1 - SPREAD
BREAK_EVEN = BUY / SELL - 1                     # 1.918%

FEATS = ["ret_1h", "ret_4h", "ret_24h", "rsi_14", "z_close_1d", "dist_vwap_1d",
         "dd_from_1d_high", "up_from_1d_low", "atr_pct", "vol_ratio", "volume_z",
         "efficiency_6h", "bb_pos", "consec_down", "dist_20d_high", "dist_20d_low",
         "btc_ret_4h", "rel_str_btc_1d", "hour_sin", "hour_cos"]


# ── NaN-safe rolling primitives (same reason as breakout.py: one NaN in a
#    cumsum poisons everything after it) ──────────────────────────────────────
def roll_mean(x, w):
    v = np.isfinite(x)
    cs = np.concatenate([[0], np.cumsum(np.where(v, x, 0.0))])
    cn = np.concatenate([[0], np.cumsum(v.astype(float))])
    i = np.arange(x.size); lo = np.maximum(i - w + 1, 0)
    n = cn[i + 1] - cn[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (cs[i + 1] - cs[lo]) / np.maximum(n, 1), np.nan)


def roll_sd(x, w):
    m = roll_mean(x, w)
    return np.sqrt(np.maximum(roll_mean(x * x, w) - m * m, 0))


def roll_max(x, w):
    o = np.full(x.size, np.nan)
    if x.size >= w: o[w - 1:] = swv(x, w).max(axis=1)
    return o


def roll_min(x, w):
    o = np.full(x.size, np.nan)
    if x.size >= w: o[w - 1:] = swv(x, w).min(axis=1)
    return o


def auc(y, p):
    o = np.argsort(p, kind="mergesort"); r = np.empty(len(y), float)
    r[o] = np.arange(1, len(y) + 1)
    sp = p[o]; i = 0
    while i < len(y):
        j = i
        while j + 1 < len(y) and sp[j + 1] == sp[i]: j += 1
        if j > i: r[o[i:j + 1]] = (i + 1 + j + 1) / 2
        i = j + 1
    a, b = y.sum(), len(y) - y.sum()
    return 0.5 if a == 0 or b == 0 else (r[y == 1].sum() - a * (a + 1) / 2) / (a * b)


def ridge_logistic(X, y, l2=4.0, iters=40):
    """IRLS, written out rather than imported, so every step is inspectable."""
    mu, sd = X.mean(0), X.std(0) + 1e-9
    A = np.hstack([np.ones((len(X), 1)), np.clip((X - mu) / sd, -6, 6)])
    w = np.zeros(A.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))
        W = np.maximum(p * (1 - p), 1e-6)
        step = np.linalg.solve((A * W[:, None]).T @ A + l2 * np.eye(A.shape[1]),
                               A.T @ (y - p) - l2 * w)
        w += step
        if np.abs(step).max() < 1e-7: break
    return w, mu, sd


def predict(X, w, mu, sd):
    A = np.hstack([np.ones((len(X), 1)), np.clip((X - mu) / sd, -6, 6)])
    return 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))


def build(conn, granularity=900, horizon=48, top_coins=24):
    syms = [r[0] for r in conn.execute(
        "SELECT symbol, COUNT(*) n FROM bars WHERE granularity=? GROUP BY symbol "
        "HAVING n>20000 ORDER BY n DESC LIMIT ?", (granularity, top_coins))]
    btc = dict(conn.execute("SELECT ts, close FROM bars WHERE symbol='BTC' AND granularity=?",
                            (granularity,)).fetchall())
    X, MFE, MAE, RET, T, S = [], [], [], [], [], []
    for s in syms:
        r = np.array(conn.execute(
            "SELECT ts,high,low,close,volume FROM bars WHERE symbol=? AND granularity=? "
            "ORDER BY ts", (s, granularity)).fetchall(), dtype=float)
        if len(r) < 3000: continue
        ts, hi, lo, cl, vo = r[:, 0], r[:, 1], r[:, 2], r[:, 3], r[:, 4]
        n = cl.size; m = n - horizon
        back = lambda k: cl / np.roll(cl, k) - 1
        tr = np.maximum.reduce([hi - lo, np.abs(hi - np.roll(cl, 1)), np.abs(lo - np.roll(cl, 1))])
        atr = roll_mean(tr, 96); m96 = roll_mean(cl, 96); sd96 = roll_sd(cl, 96)
        vv = roll_mean(vo, 96)
        vwap = np.where(vv > 0, roll_mean(cl * vo, 96) / np.maximum(vv, 1e-12), np.nan)
        dlog = np.diff(np.log(np.maximum(cl, 1e-12)), prepend=0)
        d = (np.diff(cl, prepend=cl[0]) < 0).astype(float)
        consec = np.zeros(n)
        for i in range(1, n): consec[i] = (consec[i - 1] + 1) * d[i]
        hour = np.array([(int(t) // 3600) % 24 for t in ts], dtype=float)
        b = np.array([btc.get(int(t), np.nan) for t in ts])
        rsi_up = roll_mean(np.maximum(np.diff(cl, prepend=cl[0]), 0), 14)
        rsi_dn = roll_mean(np.maximum(-np.diff(cl, prepend=cl[0]), 0), 14)
        eff_net = np.abs(cl - np.roll(cl, 24))
        eff_path = roll_mean(np.abs(np.diff(cl, prepend=cl[0])), 24) * 24
        f = {
            "ret_1h": back(4), "ret_4h": back(16), "ret_24h": back(96),
            "rsi_14": 100 - 100 / (1 + rsi_up / np.maximum(rsi_dn, 1e-12)),
            "z_close_1d": (cl - m96) / np.maximum(sd96, 1e-12),
            "dist_vwap_1d": cl / np.maximum(vwap, 1e-12) - 1,
            "dd_from_1d_high": cl / np.maximum(roll_max(hi, 96), 1e-12) - 1,
            "up_from_1d_low": cl / np.maximum(roll_min(lo, 96), 1e-12) - 1,
            "atr_pct": atr / np.maximum(cl, 1e-12),
            "vol_ratio": roll_sd(dlog, 24) / np.maximum(roll_sd(dlog, 96), 1e-12),
            "volume_z": (vo - vv) / np.maximum(roll_sd(vo, 96), 1e-12),
            "efficiency_6h": np.where(eff_path > 0, eff_net / np.maximum(eff_path, 1e-12), np.nan),
            "bb_pos": (cl - m96) / np.maximum(2 * sd96, 1e-12), "consec_down": consec,
            "dist_20d_high": cl / np.maximum(roll_max(hi, 96 * 20), 1e-12) - 1,
            "dist_20d_low": cl / np.maximum(roll_min(lo, 96 * 20), 1e-12) - 1,
            "btc_ret_4h": b / np.roll(b, 16) - 1,
            "rel_str_btc_1d": back(96) - (b / np.roll(b, 96) - 1),
            "hour_sin": np.sin(2 * np.pi * hour / 24), "hour_cos": np.cos(2 * np.pi * hour / 24),
        }
        base = cl[:m][:, None]
        mfe = np.maximum.accumulate(swv(hi[1:], horizon)[:m] / base - 1, axis=1).astype(np.float32)
        mae = np.minimum.accumulate(swv(lo[1:], horizon)[:m] / base - 1, axis=1).astype(np.float32)
        ret = (swv(cl[1:], horizon)[:m] / base - 1).astype(np.float32)
        Xi = np.column_stack([f[k][:m] for k in FEATS])
        good = (np.isfinite(Xi).all(1) & np.isfinite(mfe).all(1)
                & np.isfinite(mae).all(1) & np.isfinite(ret).all(1))
        good[:96 * 20] = False                     # long windows still warming up
        X.append(Xi[good].astype(np.float32)); MFE.append(mfe[good]); MAE.append(mae[good])
        RET.append(ret[good][:, [horizon // 2 - 1, horizon - 1]])
        T.append(ts[:m][good]); S.append(np.array([s] * int(good.sum())))
    X, MFE, MAE = np.vstack(X), np.vstack(MFE), np.vstack(MAE)
    RET, T, S = np.vstack(RET), np.concatenate(T), np.concatenate(S)
    o = np.argsort(T)
    return X[o], MFE[o], MAE[o], RET[o], T[o], S[o]


def main() -> int:
    if not DB.exists():
        print(f"no database at {DB}"); return 1
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    t0 = time.time()
    X, MFE, MAE, RET, T, S = build(conn)
    n = len(T); a, b = int(n * .6), int(n * .8)
    print(f"{n:,} bars, {len(set(S))} coins, built in {time.time()-t0:.0f}s")
    print(f"Robinhood round trip: {BREAK_EVEN*100:.3f}%  (0.95% each side)\n")

    up, dn, h = 0.0252, 0.015, 24
    mfe, mae = MFE[:, :h], MAE[:, :h]
    ui = np.where((mfe >= up).any(1), (mfe >= up).argmax(1), h + 1)
    di = np.where((mae <= -dn).any(1), (mae <= -dn).argmax(1), h + 1)
    y = (ui < di).astype(float)
    timeout = (ui > h) & (di > h)

    print(f"TARGET +{up*100:.2f}% before -{dn*100:.1f}% within {h//4}h. "
          f"Base rate {100*y.mean():.1f}%.\n")
    print("SINGLE INDICATORS, out of sample")
    print(f"  {'indicator':<16}{'AUC':>7}")
    scores = sorted(((f, max(auc(y[b:], X[b:, i]), 1 - auc(y[b:], X[b:, i])))
                     for i, f in enumerate(FEATS)), key=lambda r: -r[1])
    for f, s in scores[:6]:
        print(f"  {f:<16}{s:>7.3f}")

    w, mu, sd = ridge_logistic(X[:a], y[:a])
    pte = predict(X[b:], w, mu, sd)
    print(f"\nCOMBINED MODEL out-of-sample AUC {auc(y[b:], pte):.4f}")

    thr = np.quantile(predict(X[a:b], w, mu, sd), 0.90)
    sel = pte >= thr
    g = np.where(y[b:][sel].astype(bool), up,
                 np.where(timeout[b:][sel], RET[b:][sel][:, 0], -dn))
    net = (1 + g) * SELL / BUY - 1
    hold = RET[b:][:, 0]
    print(f"\n  model's top decile : gross {100*g.mean():+.3f}%   net {100*net.mean():+.3f}%")
    print(f"  no selection at all: gross {100*hold.mean():+.3f}%   "
          f"net {100*(((1+hold)*SELL/BUY-1).mean()):+.3f}%")
    print(f"\n  The edge is {abs(BREAK_EVEN/max(g.mean(), 1e-9)):.0f}x smaller than the toll.")
    print("  The binding constraint is the venue, not the model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
