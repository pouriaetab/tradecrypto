# The arithmetic, before anything else

*This document exists so that the first thing the project tells you is the truth, not the
thing you hoped for. Nothing here is financial advice; it is arithmetic, and you should
check it yourself.*

---

## 1. What was asked for

> $500 in, $20–$50 out per day, seven days a week.

$20/day on $500 is **4.0% per day**. $50/day is **10.0% per day**.

## 2. What those rates compound to

| Daily return | After 30 days | After 1 year |
|---|---|---|
| 4% | $1,621 | **$832,000,000** |
| 10% | $8,724 | ~$6 × 10¹⁷ |
| 0.2% (Medallion-class) | $531 | $1,033 |

The last row is the point of the table. Renaissance Technologies' Medallion fund, the best
documented track record in the history of the industry, returned roughly 66% a year net of
fees, which is about **0.2% per trading day**. The target above is 20 to 50 times that rate,
sustained, with no days off.

This is not a "hard but possible" gap. It is a "the number would have to be wrong" gap.

## 3. Why it is even harder here specifically

Your own manual observation: a coin displayed at 0.245 fills near 0.2475 on a market buy.
That is roughly **100 basis points (1.0%) of adverse selection per side**, so a round trip
costs about **2% of the traded amount** before the price has moved at all.

Run that through a scalping plan:

- 12 round trips a day at $100 each = **$1,200 of turnover**
- At 2% round trip, that is **$24/day paid in spread**
- Which is **4.8% of a $500 account, per day, in costs alone**

So a 12-trade-a-day scalping strategy on this account has to earn 4.8% per day *before* it
earns you anything. The target and the cost structure are asking for the same impossible
number twice.

Three further constraints stack on top:

1. **No shorting.** Robinhood does not permit shorting crypto. Roughly 75–80% of the signals
   a mean-reversion model generates are "fade this pump", sell signals you cannot act on.
   The backtester counts these explicitly (`dropped_short_signals`) so the loss is visible.
2. **Execution latency.** Agentic trading runs over an OAuth-gated MCP endpoint mediated by
   an LLM. Round trips are seconds, not milliseconds. Anything that needs sub-second
   execution is off the table, which rules out most of what "scalping" normally means.
3. **Small-account frictions.** Minimum order sizes and the fixed cost of being wrong matter
   far more at $500 than at $50,000.

## 4. What is actually achievable

A systematic strategy that survives honest out-of-sample testing, at retail scale, with
these costs, might produce **0.1% to 0.5% per day net** on a good stretch, with losing weeks
and a real chance of a 10–20% drawdown. That is a genuinely good outcome. On $500 it is
**$0.50 to $2.50 a day**, and most of that is noise until you have hundreds of trades.

To reach $20–$50/day, the lever is not a better model. It is capital:

| Realistic net daily edge | Capital needed for $20/day | for $50/day |
|---|---|---|
| 0.5%/day (excellent) | $4,000 | $10,000 |
| 0.2%/day (Medallion-class) | $10,000 | $25,000 |
| 0.1%/day (good retail) | $20,000 | $50,000 |

And you should not put $20,000 behind an edge you have not proven with $500 first.

## 5. So what is the $500 for?

**Buying information, not income.** The $500 pays for the answer to one question:

> Does any signal I can compute produce more gross edge than Robinhood's ~2% round-trip
> cost, reliably, out of sample?

This system is built to answer that question rigorously and cheaply, and to tell you *no*
when the answer is no, which is the likely answer for short-horizon scalping specifically,
and the answer most trading software is designed never to give you.

If the answer turns out to be yes for some strategy at some horizon, then you scale capital
into it deliberately, with the position sizing already built in. If it is no, you have spent
a small amount of money to avoid a large mistake, and the measurement machinery is reusable.

## 6. The honest sequencing

1. **Measure the real cost.** Log 20+ real Robinhood fills. Until then every number
   downstream is leaning on a prior. (Cost Lab page.)
2. **Collect history.** The engine stores every candle it fetches. Backtests are meaningless
   until there are weeks of it.
3. **Run the cost-sensitivity curve.** If the strategy is unprofitable at your measured cost,
   stop there. Do not tune parameters until it looks profitable, that is exactly the
   behaviour the deflated Sharpe and PBO machinery exists to catch.
4. **Walk-forward validation.** Purged, embargoed, with every configuration counted.
5. **Paper trade for 200+ trades**, with live quotes and simulated costs.
6. **Only then**, consider $50–100 of real money, in advisory mode, where you approve each
   order. Full autonomy is the last step, not the first.

There is no step in that list that can be honestly skipped, and the system will not let you
skip them silently: every gate is enforced in code and shown on the dashboard.

## 7. Risk of ruin, stated plainly

Automated trading of volatile assets with leverage-free but high-cost execution can lose the
entire stake. Bugs, stale quotes, exchange halts, and a strategy that stops working are all
routine, not exotic. The default configuration risks at most $15/day and halts itself at a
10% drawdown, and those defaults exist because they are the difference between an experiment
and an expensive lesson.
