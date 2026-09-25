# TradeCrypto

> ## ⚠️ Read [DISCLAIMER.md](DISCLAIMER.md) first
>
> Personal research project, shared **as-is**, under the MIT License — **no
> warranty, and the author is not liable for anything, including trading
> losses.** It is **not financial advice**.
>
> **It loses money.** That is the measured result, not modesty: a Robinhood
> round trip costs about 1.9% and most of these coins move 2–3% in a day, so the
> cost eats the edge. Over a month of real replayed prices the book is down.
>
> It trades **pretend money by default**. Making it use real money takes three
> deliberate acts by whoever runs it, and no button in the app can do them. If
> you take those steps, the risk and the outcome are yours.


A crypto trading bot with a web page that shows every decision it makes and why.
Runs on **fake money against real live prices** — nothing is at risk.

### Which file do I read?

| you are | read this |
|---|---|
| **new here, want to run it** | **[START-HERE.md](START-HERE.md)** |
| already running it, an update was sent | [HOW-TO-UPDATE.md](HOW-TO-UPDATE.md) |
| sharing it with someone | [OWNER-SETUP.md](OWNER-SETUP.md) |
| want the settings explained | [SETUP.md](SETUP.md) |

---

## About the project

A Robinhood crypto trading system built so that **every number on screen can be traced to the
model, the data, and the assumptions that produced it**. No black boxes, and no number without
its sample size.

> **Read [`docs/EXPECTATIONS.md`](docs/EXPECTATIONS.md) first.** It contains the arithmetic on
> what a $500 account can and cannot produce, and it is the reason this project is built as a
> measurement instrument before it is built as a money machine.

## Quick start

```bash
cd /path/to/tradecrypto
./run.sh                 # backend :8006, dashboard :5180
./run.sh --selftest      # verify feeds, database, models and statistics without trading
```

Or launch it from Control Deck (registered as **TradeCrypto**, category Trading).

Default mode is `paper`. Real orders are structurally impossible until `.env` contains
`TC_EXECUTION_MODE=mcp` **and** `TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`.

## The idea

Robinhood's agentic trading is an OAuth-gated **MCP server**
(`https://agent.robinhood.com/mcp/trading`) — not a keyed REST API. It can place crypto orders,
but it cannot short, it keeps no history for research, its latency is a network round trip, and
its costs are embedded in the spread. Those four facts shape everything here:

- **Research runs on a free public exchange feed** (Coinbase, cross-checked against Kraken),
  and the engine continuously measures the *basis* between that feed and Robinhood's real fills.
  That basis is not noise — it is the cost of trading, and it is the number that decides whether
  any strategy is viable.
- **Execution is pluggable**: `paper` (simulated fills degraded by the measured cost model),
  `advisory` (the engine emits an order ticket a human or a Claude session executes), and
  `mcp` (direct, gated behind explicit confirmation).
- **Nothing trades without clearing the cost hurdle.** Every signal carries an expected edge with
  a confidence interval; if the interval includes zero, the edge is set to zero and no order is
  placed.

## The dashboard

| Page | What it answers |
|---|---|
| **Overview** | What mode am I in, what has the engine done, how much risk headroom is left |
| **Trade Desk** | Advisory mode: the engine posts an entry ticket, you execute it by hand, you report the fill, and it posts the exit ticket when the time comes. Also where you set a **trade budget** ("take 2 trades today") |
| **Ledger** | Per-strategy P&L book with a paper / advisory / live toggle, a date filter, daily P&L bars and a cumulative curve |
| **Attention** | Which one or two coins are getting today's attention, and whose run is already late |
| **False Breakouts** | The trap detector: level broke — is it real? Coefficients, calibration, and a live per-coin check |
| **News & Research** | Market and per-coin news used defensively, plus every paper behind this system and what it's used for |
| **Universe** | Why each coin is tracked, traded or dropped — every gate with its actual value; plus macro series and their measured correlation to BTC |
| **Automation** | Every research job, when it last ran, what it found, and pause / resume / run-now |
| **Data** | Multi-year backfill, coverage per coin, storage projections, and a raw-bar browser with date filters |
| **Model Lab** | One strategy end to end: data, train/validation/test split, what was fitted, every acceptance gate, verdict, and what would have to change |
| **Cost Lab** | What a round trip actually costs — globally and **per coin** — the number everything else depends on |
| **Movers** | The top-movers screen, with each row's spread next to its move |
| **Research** | Cost sensitivity → backtest → walk-forward, in that order |
| **Strategies** | Each hypothesis, its fitted coefficients, and its earned capital allocation |
| **Models** | Every model card: formula, assumptions, failure modes, citations |
| **Journal** | Full audit trail, including the signals that were rejected and why |
| **Risk** | Live guard status and the kill switch |

Any number with a **"how is this computed?"** button opens the model card behind it.

## The four strategies

The operator's own ideas, each a falsifiable hypothesis with the same gates — see
[`docs/STRATEGIES.md`](docs/STRATEGIES.md):

- `fast_flip` — quick in and out, 5–30 minutes. Included as the **control**.
- `forced_momentum` — hold longer when the move is efficient (Kaufman Efficiency Ratio) and
  volume confirms it.
- `regime_swing` — trade only when breadth **and** the leader agree the market is risk-on;
  the "early morning" filter is applied only for hours that survive FDR correction.
- `lead_lag_rotation` — be early to the coins that follow BTC/ETH, where the lag is
  statistically established rather than assumed.

## Statistical machinery

Implemented and unit-tested in `backend/app/research/stats.py`:

- **Probabilistic and Deflated Sharpe** (Bailey & López de Prado 2012, 2014) — corrects for
  skew, kurtosis, and for how many configurations were searched.
- **Probability of Backtest Overfitting** via CSCV (Bailey, Borwein, López de Prado & Zhu 2017).
- **Stationary block bootstrap** (Politis & Romano 1994) with **BCa** intervals (Efron 1987).
- **Minimum track record length** — how many trades before a Sharpe means anything.
- **Bayesian edge posteriors** (Normal-Inverse-Gamma, Beta-Binomial) driving capital allocation.
- **Fractional Kelly on the lower confidence bound** of the edge, never the point estimate.
- **Assumption tests** (Jarque-Bera, Ljung-Box, Levene) surfaced next to the results they qualify.
- **Benjamini-Hochberg FDR control** for calendar effects and lead-lag discovery, because 168
  hour/weekday buckets and 500 (coin, lag) pairs guarantee false positives at any fixed alpha.
- **Per-coin cost estimation** — a log-log model of Robinhood's markup on reference spread,
  volatility, liquidity and tick size, with empirical-Bayes shrinkage toward real fills.

## Layout

```
backend/app/
  config.py              all risk limits; live trading gated here
  core/db.py             SQLite schema — the single source of truth
  core/registry.py       model cards (the anti-black-box layer)
  data/feeds.py          Coinbase + Kraken adapters, cross-checking
  data/universe.py       tradeable universe + movers screen
  execution/cost_model.py  the Cost Lab
  execution/broker.py    paper / advisory / MCP brokers
  execution/engine.py    the live loop
  strategy/              falsifiable hypotheses, one file each
  research/stats.py      the statistics
  research/backtest.py   event-driven backtest, walk-forward, cost sensitivity
  risk/guards.py         hard pre-trade checks + kill switch
  feedback/loop.py       reward/penalty attribution and capital reallocation
frontend/src/            React + Vite dashboard
docs/EXPECTATIONS.md     the arithmetic
```

## Getting history

```bash
# from the Data page, or:
curl -X POST 127.0.0.1:8006/api/v1/backfill/start -H 'content-type: application/json' \
     -d '{"granularity": 3600, "days": 1460}'
```

Roughly 9,000 requests, 15-25 minutes, ~364 MB for four years of hourly bars across 80
coins. Daily is instant and tiny. **One-minute data cannot be backfilled from any public
venue** — it is collected going forward only, which is a real constraint on the fast
strategies and an argument for working on the longer-horizon ones first.

## Connecting Robinhood

The agentic endpoint is an OAuth MCP for an interactive client, so this app cannot call
it. The bridge runs the other way: a Claude Code session holding the MCP reads Robinhood
and POSTs into the local API. Full instructions, including the exact prompts to paste, are
in [`docs/ROBINHOOD_SYNC.md`](docs/ROBINHOOD_SYNC.md). Nothing on that path can place an order.

## Does the laptop have to stay on?

Depends on which part you mean.

| | Needs the app running? |
|---|---|
| **Backfilled history** (daily / hourly / 15-min) | **No.** One download, stored on disk forever. Sleep, reboot, close the lid — it stays. |
| **1-minute bars going forward** | **Yes.** Public feeds don't sell minute history, so the only way to have it is to be running when it happens. |
| **Paper trading and P&L** | **Yes.** The engine books trades only while it runs. |
| **Live quotes, signals, tickets** | **Yes.** |

So the swing and rotation strategies can be researched entirely on backfilled hourly data with
the laptop off. Only the fast strategies and live paper-trading need uptime.

macOS sleeps aggressively on battery. To keep a session running:

```bash
caffeinate -dimsu     # in its own terminal; Ctrl-C to release
```

Gaps from sleep are not silently smoothed over — the Data page shows a **completeness %** per
coin so missing stretches are visible.

## Running it reliably (and not filling your disk)

```bash
./scripts/install-autostart.sh      # start at login, restart automatically if it dies
./scripts/install-autostart.sh remove
tail -f logs/tradecrypto.log
```

That installs a macOS LaunchAgent pointing at `scripts/supervise.sh`, which restarts the app
on any unexpected exit, backs off if it is crash-looping, and rotates its log at 20 MB.

**Memory.** The process watches its own RSS and exits with code 3 at `TC_MEMORY_CEILING_MB`
(default 1500). A clean exit is restartable; an OS kill is not. Overview → Health shows
current, peak and MB/hour growth — a rate that does not flatten means something is
accumulating.

**Disk.** A daily `housekeeping` job prunes 1-minute bars after 45 days and quotes after 14,
checkpoints the WAL, and compacts the file when it is worth it. **Hourly and daily bars are
kept forever** — that is the history everything is validated against, and it grows slowly.
Preview what retention would remove with `GET /api/v1/housekeeping/preview`.

## Kill switch

```bash
touch data/KILL_SWITCH     # nothing can trade
rm data/KILL_SWITCH        # released
```

It is a file, not a flag, so it works when the web UI does not, and it survives a restart.
