# The four strategies, and what the machine says about them

These are the operator's own four ideas, each turned into a falsifiable hypothesis with a
fitted coefficient, a confidence interval, and the same acceptance gates. Every one of them
opens in **Model Lab**, where you can see its split, its calibration, its gates and its
verdict.

| # | Strategy | The idea, in your words | How it is measured |
|---|---|---|---|
| 1 | `fast_flip` | "quick buy and sell, 5–30 min, anything going up" | short-horizon momentum + volatility band. Deliberately the **control**. |
| 2 | `forced_momentum` | "hold longer when the move is being forced up" | Kaufman Efficiency Ratio (net travel ÷ total path) + volume expansion + run length |
| 3 | `regime_swing` | "when the whole market is alive like BTC 60k→80k, get in early and hold" | breadth **and** leader trend must both agree; hour-of-day filter only from buckets that survive FDR correction |
| 4 | `lead_lag_rotation` | "the big ones move first, then the next batch" | lagged cross-correlation vs a BTC/ETH basket, Bartlett SE, Benjamini-Hochberg across every (coin, lag) pair |

## What the break-even test already shows

On a synthetic market built to have a known lead-lag structure and a mild uptrend — a
smoke test, not a forecast — the cost-sensitivity curves ranked exactly as the theory
predicts:

| Strategy | Hold | Break-even cost (per side) |
|---|---|---|
| `fast_flip` | 20 min | ~10 bps |
| `forced_momentum` | 45 min | ~20 bps |
| `lead_lag_rotation` | 60 min | ~40 bps |
| `regime_swing` | 4 hours | ~100 bps |

**The pattern is the finding: the longer you hold, the more spread you can survive.** A move
has to be big relative to the round trip, and time is what makes moves big. Robinhood's
estimated cost is roughly 80–100 bps per side, which is why the fast ones sit below the line
and the swing one sits near it.

This is your own intuition ("fast scalping mostly doesn't work on this platform") coming out
of the arithmetic rather than out of memory — and it says where to spend effort: the swing
and rotation ideas, not the flip.

## What is not claimed

Those break-even numbers came from synthetic data used to test that the plumbing works. None
of the four has been calibrated on real Robinhood-tradeable history yet, because there isn't
enough of it stored yet. Every model card says `exploratory` and every strategy currently
reports **no edge claimed**, which is why nothing trades. That is the system working, not
failing.

## The order to work through them

1. Let the engine collect a week of bars.
2. Run **Model Lab → `regime_swing`** first — it has the most room above the cost line.
3. Then `lead_lag_rotation`; check how many followers survive FDR correction. If the answer
   is zero on real data, the batch-rotation idea is not there, and that is worth knowing in
   an afternoon rather than after three months of trading.
4. Run `fast_flip` last, as the control. If it clears the gates, something is wrong with the
   cost model and you should distrust the rest.

## pump_catch — the same rule on a faster clock (2026-09-19)

`pump_catch` is `volume_build`'s trigger (1.5× the coin's 14-day hourly volume
normal, +3% over two hours, a decisive 48-hour trend, not red today, not already
extended) evaluated on a **rolling 60-minute window of 15-minute bars** that the
engine refreshes every five minutes, instead of the forming calendar hour
refreshed every ten. It exists because PENGU was bought at 12:24 — the top of a
run that started at 11:39 — and every open over the previous three days had
landed 1–57 minutes after its hourly bar.

It is not a different rule. `tests/test_pump_catch.py` proves parity: the same
synthetic move fires on both clocks with the identical exit contract, and the
rolling one fires first. Whether that lead is worth having is decided by
`research/rolling_entry.py` (job `rolling_entry`, daily; card on the Strategies
tab): first run, 190 days × 30 coins — on the 47 moves both clocks caught, the
rolling clock bought a median 30 minutes earlier and 0.31% lower and netted
+0.81%/trade against +0.14%; but it also made 64 entries the hour never made,
at −0.80%/trade, and missed 61 the hour made. Net of everything the two clocks
were level (−0.12% vs −0.13%). So: **earlier, yes; without more false entries,
no — not yet.** It runs in paper beside volume_build so the lab can keep score
on real fills, and the extra entries carry their features so the retrainer can
look for what separates them.

## burst_catch — the first hour of a burst, small target, hard clock (2026-09-21)

Written from 2026-09-20 10:30–12:00: twelve coins ran 3–12% inside an hour and the desk
bought all twelve at 11:41–12:11, at the top, because every rule needs a climb that is
hours old (day_climb +5%/4h, volume_build +3%/2h in a two-day trend, pump_ride +18%/4h).
From the minute bars, eleven of twelve reached +2% within 60 minutes of the run starting
and six reached +4% (NEAR +12.5, SUI +8.6, ENA +7.9, SEI +5.6, ZRO +3.7, OP +3.6, WLD
+3.5 …). Against a 1.92% round trip a +2% target nets nothing; +4% nets ~2% on half.

The rule, on 15-minute bars: close ≥ +3% above the close 3 h ago AND the close 1 h ago was
still under half of that (the rise is inside the last hour); the last hour's volume ≥ 1.5×
the 14-day per-bar normal; the last bar itself ≤ +4% (the spike bar is not chased). Exit:
target +4% gross, stop −3%, hard limit 90 minutes. Every threshold is a grid starting point.
Expected edge 0; a paper experiment judged by the lab on replay and on fills, like the rest.
`tests/test_burst_catch.py`: fresh burst fires; spike bar not chased; hour-old rise ignored;
no volume, no signal. Live in the roster since 2026-09-21 08:54 (paper only).

## Prediction markets as context (2026-09-21)

Polymarket's crypto markets (Gamma API, free, no key; the default Python user-agent gets a
403, a named one is fine) are fetched hourly by the `prediction_markets` job into
`prediction_markets`. The same-day "above $X" ladder is read as a distribution: the strike
where the odds cross one half is the implied close, the 10%/90% strikes the implied range.
First read, 10:39 on 2026-09-21: BTC implied close $85,538, range $84,301–$87,438, 11 strikes.

What it is for: a candidate regime feature ("what does the crowd expect of BTC today and this
month"), to be judged by `regime_days` at the same 2σ bar as breadth and dispersion before it
changes any size. What it is not: an entry input. The markets are BTC/ETH only and resolve at
day and month ends; nothing in the execution, strategy or risk path reads the table. Universe
tab → "What the crowd expects". `tests/test_prediction_markets.py` pins the parser, the ladder
read, and a fetch against a canned payload.
