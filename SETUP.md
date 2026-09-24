# TradeCrypto — setup

An automated crypto trading bot with a web app that shows every decision it
makes. It runs in **paper mode** by default: real live prices, simulated fills,
no money at risk and no account connected.

Everything below assumes macOS or Linux, Python 3.11+ and Node 18+.

---

## 1. Run it

```bash
cd tradecrypto
cp .env.example .env
./run.sh
```

Then open **http://127.0.0.1:5180**

`run.sh` creates its own Python environment, installs what it needs, builds the
front end and starts both halves. The database is created empty on first run, so
your numbers start the day you start.

That is the whole setup. **No accounts, no API keys, no credentials.**

### Running a second copy alongside an existing one

If you already have TradeCrypto running, the test copy will fight it for ports
and for its database. Give the copy its own:

```bash
cd ~/tradecrypto-test
cp .env.example .env
cat >> .env <<'EOT'
BACKEND_PORT=8106
FRONTEND_PORT=5280
ALLOWED_ORIGINS=http://127.0.0.1:5280,http://localhost:5280
TC_DB_PATH=./data/tradecrypto.sqlite
TC_TUNNEL=0
TC_LAN=0
EOT
./run.sh
```

Then open **http://127.0.0.1:5280**. The two copies never touch: different
ports, different database file, and the second one has phone access off so it
cannot claim the tunnel.

---

## 2. Where the prices come from

Three options. The first needs nothing from you and is already switched on.

### Option A — public exchange data (default, recommended)

Live prices come from **Coinbase's public API**, with **Kraken** as a fallback.
Both are free and neither needs an account, a key or a login.

Already set in `.env.example`:

```
TC_PRIMARY_FEED=coinbase
TC_FALLBACK_FEED=kraken
```

Swap the two lines if you prefer Kraken as the main feed.

**What you lose by not connecting Robinhood:** only the *measured* bid-ask
spread. Without it the app assumes **0.95% per side** and labels that number
"not verified" everywhere it appears, so you always know which figures rest on
an assumption. Prices, strategies, signals, the research lab and every chart are
identical.

### Option B — connect Robinhood for live trading

Only needed if you want it to place real orders. Robinhood's agentic trading is
an OAuth-gated MCP server, so you authorise it through Robinhood and it writes a
token file — you never put a password into this app.

```
TC_EXECUTION_MODE=mcp
TC_RH_TOKEN_PATH=./secrets/robinhood_mcp_token.json
TC_LIVE_CONFIRM=<the exact phrase the app asks for>
```

Live trading is impossible unless `TC_LIVE_CONFIRM` is filled in. Leave it empty
and the app physically cannot risk money.

### Option C — Robinhood username and password

**Not supported, on purpose.** This app never asks for, stores or transmits an
account password. Robinhood does not offer a public REST API for retail
accounts, so anything that "logs in" for you is scraping a session — which
breaks their terms, can get an account locked, and means handing credentials to
software. Use Option A for data and Option B if you want to trade.

---

## 2b. On a phone

There is no app to install — the web app is built for a phone screen and you
open it in the phone's browser. Two ways, both set in `.env`:

| setting | what you get |
|---|---|
| `TC_LAN=1` | reachable from the same wifi only |
| `TC_TUNNEL=1` | the machine dials out to Cloudflare and you get an `https://` address that works on cell data, through a VPN, anywhere |

The tunnel needs `brew install cloudflared` once. It is token-gated: the token
lives in `secrets/lan_token.txt`, is generated on first run, and anyone without
it gets a 401. **Never share that token.**

Leave both at `0` and the app is localhost-only.

---

## 3. Paper vs real money

| `TC_EXECUTION_MODE` | what happens |
|---|---|
| `paper` (default) | real prices, simulated fills, nothing at risk |
| `advisory` | the engine writes order tickets; a human executes them |
| `mcp` | the engine places real orders through Robinhood |

Anything other than `paper` also requires `TC_LIVE_CONFIRM`. **Run it in paper
for a few weeks before considering anything else.**

Set your starting balance in `.env`:

```
TC_ACCOUNT_EQUITY=2000
```

---

## 4. Things worth knowing

1. **The spread is the whole game.** On Robinhood a round trip costs about
   1.9%. Most crypto moves 2–3% a day. Every strategy in here has to clear that
   before it makes a cent — read `docs/` for what has and has not worked.
2. **Positions are correlated.** Holding eleven coins is not eleven bets; they
   move together. The app measures this and shows the effective number.
3. **The lab runs itself.** Every few hours it scores each strategy and switches
   off the ones that are clearly losing. Your own on/off switch always wins.
4. **Nothing here is advice.** It is a research tool that happens to be able to
   trade. Read `CLAUDE.md` for the rules the codebase is built on.

---

## 5. If something breaks

```bash
./run.sh stop
./run.sh
bash scripts/gate.sh --fast      # runs the full test suite and health checks
```

The app keeps a plain-English event log at **Sources → events** in the web UI —
start there before reading any code.
