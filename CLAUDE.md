# Glassbox — development context

## Purpose
Automated Robinhood crypto trading with a transparency-first dashboard. The design constraint
that drives everything: **no number is displayed without the model, sample size, and assumptions
behind it.** Read `docs/EXPECTATIONS.md` before changing anything about targets or sizing.

## Tech stack
- **Backend**: FastAPI + Uvicorn, Python >= 3.11, SQLite (WAL), numpy/scipy/statsmodels, httpx
- **Frontend**: React 18 + Vite 5, plain CSS with tokens mirroring `control_deck/DESIGN_SYSTEM.css`
  (deliberate deviation from the Tailwind default in TECH_STACK_STANDARDS: no extra build step,
  and the token values match the shared palette exactly)
- **Ports**: backend `8006`, frontend `5180` (next free pair after QuantDesk 8004/5178 and
  TradeForge 8005/5179)

## Safeguards

`docs/SAFEGUARDS.md` indexes every safeguard in this repo against the incident that earned
it, and points at the full portable blueprint. Read it before designing anything new in the
persistence, risk, access or learning paths.

## Rules of investigation (read before diagnosing anything)

These are not style preferences. Every one of them is here because breaking it cost
real time on a real day, and the numbers below are what actually happened.

**These are mandatory, not advisory.**

- **`scripts/gate.sh` — before you change anything, and again before you hand it back.**
  It machine-checks every rule below that can be checked, runs all four existing
  checkers and the test suite, and verifies the housekeeping the safety nets depend on
  (disk headroom, backup age, ledger freshness). It exits non-zero when something is
  wrong. `--quick` skips the suite. The rules it cannot check are printed at the end as
  a list to confirm by hand — read them, do not scroll past them.
- **`scripts/facts.sh` — before you diagnose anything.** It answers "what is true right
  now": which boot is current, what the phone is getting and what that status means,
  who holds the database, which jobs have drifted. Every log fact in it is scoped to the
  current boot *by construction*. Use it instead of assembling the same picture by hand,
  badly.
- **Every repeat of a mistake earns an entry in `gate.sh`.** A rule that lives only in
  this document gets re-learned the expensive way. If it can be checked, add a
  `rule_<id>()` function and list it in `RULES`; if it genuinely cannot, add a line to
  `MANUAL`. Keep the incident in the comment — a rule without its story gets deleted by
  someone who mistakes it for pedantry.

0. **Read the shared checklist before designing or changing anything.**
   `../webapp_blueprint/CHECKLIST.md` (mounted at `market/webapp_blueprint/`) is
   100 rows of things that have already gone wrong across TradeForge, Glassbox,
   Trade Guard, Looper, trade_mirror, trade_genai and Control Deck. Sections 1, 2
   and 4 catch the most. This is not optional reading: on 2026-09-18 this project
   independently rediscovered two rules that were already written there —
   `kill -0` reporting a zombie as alive (1.6) and WAL still fsyncing on every
   commit without `synchronous=NORMAL` (4.20). Both were then imported. Rediscovery
   costs a session; reading costs ten minutes.

0b. **Downtime is a cost, and quality outranks it.** The desk exists to collect
   data and trade; every second it is down is data it cannot get back. So:
   prefer changes that need no restart, make the restarts that are needed as
   short as possible, and MEASURE the gap rather than assuming it is small
   (2026-09-19: 8s per code update, 5s of which was a pause a planned restart
   did not need — now 0). But this rule NEVER outranks correctness. If
   something has to come fully down to be tested or fixed properly, take it
   down and say how long for. A desk that stayed up through a change nobody
   verified is worse than one that was off for ten minutes.

   What a gap actually costs, so the trade-off is real and not a feeling:
   **bars refetch themselves** from the exchange (`minute_topup`,
   `history_topup`), and **the live mid does not** — quotes are written only
   while the process is up, so a gap in them is permanent. Weigh a restart
   against the quotes it will cost, not against a vague sense of uptime.

1. **A log line proves nothing until you know which boot it belongs to.**
   `logs/tradecrypto.log` is 22 MB and spans sixteen restarts. On 2026-09-18 the line
   `phone access is ON (same address as before)` was quoted back to the operator as
   proof that phone access was on. It was a real line from an earlier boot; the current
   boot had never printed it, and the advice that followed — "don't press the Phone
   button" — was exactly backwards. Find the last `starting backend on` and read only
   from there. If it is not in the file the log has rotated: say UNKNOWN and go look in
   `tradecrypto.log.1`. Never widen the search to the whole file, because a fact from
   the wrong boot reads exactly like the truth.

2. **Every claim ships with the evidence that produced it, and a named value gets
   checked twice.**
   Not "the phone is probably being refused for the token" — run it, read it, quote it.
   And when the answer is a specific value, confirm it is that value: *is the phone
   getting 401?* → check → *no, 403* → check again that it really is 403 and not a 401
   further down, because the two mean opposite things. 401 is a wrong token and a paste
   fixes it; 403 is a backend bound to localhost that never looks at a token at all, and
   the same screen for both sent the operator round in a circle for a morning.
   State the evidence in the answer: `297 × 403 from 104.241.55.49 since this boot`
   is a fact. "It seems to be a token problem" is a guess wearing a fact's clothes.

3. **A checker that already exists beats grepping by hand.**
   `scripts/preflight.sh`, `scripts/render-check.mjs`, `scripts/check_css_vars.py`,
   `scripts/check_jsx_imports.py`, `scripts/facts.sh`, and `pytest tests/`. On 2026-09-18
   a token screen was written using `var(--good)` and `var(--bad)`, neither of which
   exists in this app — found by manual grep, while `check_css_vars.py` sat unused.

4. **Prove a fix by re-breaking it.**
   Write the test, watch it pass, then deliberately revert the mechanism and watch it go
   red, then restore. A test that passes both ways tests nothing. Two of the five
   database-safety mechanisms added on 2026-09-18 were only proved this way; one of the
   tests turned out to be stubbing out the very code it claimed to check.

5. **From anywhere that is not the app, the live database is opened `immutable=1`
   or not at all.** SQLite's locking does not cross the bridge mount, so a second
   writer on another machine destroys the file — that is how 18 hours of trading was
   lost on 2026-09-18. **`mode=ro` is not sufficient**: a WAL database is read through
   a shared `-shm` index that every reader maps *and writes to*, so a read-only
   connection still touches files the running app depends on. Later the same day, a
   test run opening the live database `mode=ro` from the VM coincided with a
   three-second burst of `file is not a database` in the app, on a file that was
   itself intact. Use `file:...?mode=ro&immutable=1` (skips WAL and the -shm
   entirely), or read a `data/ledger/` snapshot, or work on a copy. `TC_DB_READONLY=1`
   now implies immutable. `dbguard.claim()` blocks the write path; do not route around
   either.

   **Third incident, 2026-09-20 16:14:** an `immutable=1` read of the live file from the
   VM (a 35,000-bar panel build) ran across a planned restart; the VM side got
   `database disk image is malformed` and the app died with `Bus error: 10` in its
   first tick. The file was intact (`[db] database ok` on the next boot). Correlation,
   twice now, is enough: **from the VM, heavy reads of the live file are not done at
   all.** Read `data/ledger/` snapshots; anything that needs the bars runs as a
   scheduler job on the Mac and is read back from `runs`.

   **And know what immutable costs you:** it skips the WAL, so it shows only the main
   file — every write since the last checkpoint is invisible. On 2026-09-18 that made a
   newly created table look as though its migration had never run. When you need the
   *current* state, read the newest `data/ledger/` snapshot, not the live file.

   **Fourth incident, 2026-09-21 09:08:** `scripts/facts.sh` — our own tool — opened the
   live file `mode=ro` from the VM for its "scheduled jobs" section. Twelve seconds
   later the Mac backend died with `Bus error: 10`, mid-boot catch-up, 946 s into a
   boot. Nothing heavy: one SELECT over 28 rows. So it is not the size of the read;
   it is the -shm mapping, and `immutable=1` was not enough on 09-20 either. The rule
   is now absolute and mechanical: **no process on the VM opens `data/tradecrypto.sqlite`
   in any mode.** `facts.sh`, `gate.sh` (liveness and the test suite) all detect that
   they are off the holding machine and read the newest `data/ledger/` snapshot
   instead (`TC_DB_PATH`). Tests that need `bars`/`quotes` skip there with the reason
   (`skip_without()` in `tests/conftest.py`) and run in full on the Mac. When the VM's
   shared disk is full (it was, 0 MB, the same day), run the suite in the cloud
   container against a staged snapshot — 342 passed, 2 skipped, 34 s.

6. **On the Linux side, redirect the bytecode cache.**
   `export PYTHONPYCACHEPREFIX=$HOME/scratch/pycache`. `.pyc` files inside the mounted
   folder cannot be deleted from there, and a restored source file whose mtime lands in
   the same second as the stale cache is silently ignored — a fix looks dead, or worse, a
   broken build looks fixed. The tell on 2026-09-18 was a response carrying status 401
   with the 403 body: a combination the source cannot produce.

6b. **From the Linux side, nothing that creates-then-deletes inside a mounted
   folder — `git` above all.** The VM cannot unlink on the mount, so a `git
   commit` run from there succeeds and leaves `index.lock`, `HEAD.lock` and
   `tmp_obj_*` behind, which then block every commit the Mac tries to make.
   2026-09-19: one test run of `vault_mirror.sh` from the VM did exactly this to
   the vault's repo; it took a delete grant to clean up. Read git state from the
   VM freely (`git log`, `git status`); run anything that writes it on the Mac.

7. **Say when you were wrong, in the same message that corrects it.**

8. **Adding to the blueprint is not the operator's job — and there are TWO of
   them.** After any incident — data lost, a wrong diagnosis, a guard that did
   not hold — update `docs/SAFEGUARDS.md` and the project blueprint *without
   being asked*, in the same session, while the detail is still exact. **And if
   the root cause is not specific to this app** — launch/ports/process,
   verification, data honesty, live-loop or signal behaviour, dependencies,
   assistant workflow — add a row to `../webapp_blueprint/CHECKLIST.md` in the
   same session too: the rule, U or C, the project and date it bit us in with
   the actual symptom, and what handles it now. A fix that stays in this repo
   only protects this repo. If the fix is in a scaffold file, put the same change
   in `../webapp_blueprint/templates/`. Waiting to be told is how the lesson gets
   lost.

9. **A restore is not finished when the file opens.** Restoring rolls
   `sqlite_sequence` back with the data, so the next rows reissue ids that already
   belonged to real ones — on 2026-09-18 four trades from the 17th had their ids
   handed to four different trades, silently. `enforce_trade_id_high_water_mark()`
   runs at boot *and* every ten minutes, because a mark taken only at startup is
   stale the moment a trade closes. Recovered data is matched on
   `(symbol, strategy, ts_open)` and **never on id**.

10. **A restore resurrects positions that had already closed.** The backup shows
   them open; the engine closes them again at today's price and writes a trade
   that never happened, on the wrong day, with the wrong number. ZEC really closed
   09-17 09:15 for $9.03 and was recorded as 09-18 00:15 for $24.56. A wrong trade
   is worse than a missing one — it feeds P&L, the posteriors, sizing and the exit
   study. After any restore, reconcile open positions against reality before
   trusting a close.

11. **Any number you quote about lost or changed data was counted, not
    remembered.** "Today's 10 trades" was said from memory in the same message
    that introduced the rule about quoting evidence. It was four trades, on the
    17th, not the 18th. Count it, name the source, or do not say it.

12. **A destroyed SQLite file is not necessarily lost data.** The 2026-09-18 file
    had no header anywhere in 450 MB and SQLite would not touch it, but the B-tree
    leaf pages were intact and `scripts/salvage_trades.py` pulled every row back
    out by decoding the record format directly. Look before concluding.

15. **A guard the operator switches off must not re-arm on the same fact.** 2026-09-23: the
    daily-loss switch was released nine times in four hours and re-engaged within minutes each
    time, reading the same loss. The release is now the decision: past the cap, it waives the cap
    for the rest of that day (`app_state.daily_loss_cap_waived_for_day`), says so in the event log
    and on the Risk tab, and expires at midnight. Drawdown and per-order guards are untouched.
    And **a `.env` edit is a code edit** — inert until a restart — so `code_version` watches it and
    autoapply restarts for it (the operator raised the cap at 16:51; the process ran the old one
    until 18:51).

14. **One database lock, one lane: a route the UI polls never runs a full-table scan.**
    2026-09-21 10:10: the banner said "backend not answering" for forty minutes while the
    backend was up. `/mode` (one row) took 27 s; `/health/report`, which touches no database,
    took 24 s; one tick took 365 s. `GET /system/threads` (added then) showed the lock held by
    API threads running `COUNT(*)` and `MIN/MAX(ts)` over the 3-million-row `bars` table —
    `/system/status` and `/setup`, polled by every open tab every few seconds — 3,577
    acquisitions and 354 s of cumulative waiting in the first ninety seconds after a boot.
    Every query in the app queues on `db._LOCK`, so a 1.5-second scan issued four times a
    minute from three tabs is a queue, and the engine's tick stands in it. Now:
    `db.query_one_cached` shares those answers for two minutes, today's report rebuilds at
    most every two minutes, cost observables every five, and
    `tests/test_polled_routes_share_scans.py` pins each one. After: 6,513 acquisitions,
    48 s waited, mean 7 ms; `/mode` 0.56 s, ticks 2–3 s. **Before saying "slow", read
    `/system/threads` (Data tab → "Who holds the database lock"): it names the holder, the
    statement, and how long callers have queued since boot.** The banner now distinguishes
    "answering slowly" from "not answering".

13. **A test that resolves a path inside the project can stop the live desk.** On
    2026-09-21 08:37 `test_a_loss_already_taken_today_shrinks_the_headroom` built a
    $150 loss in its private temp database and called `pre_trade_check`. The guard did
    its job — crossed the cap, engaged the kill switch — and wrote `data/KILL_SWITCH`
    at the hardcoded project path, through the mount, on the running desk. No new
    position for the rest of the morning; realised P&L that day was +$7; the CRITICAL
    event went into the temp database, so the live log never said why. The database
    was isolated, the vault was isolated; the one path nobody had listed was not.
    Now: `TC_KILL_SWITCH_FILE` is a setting, `conftest.py` forces it into the suite's
    temp directory, the session guard refuses to run if it still resolves inside
    the project, and `tests/test_kill_switch_isolation.py` trips the cap on purpose
    and checks the desk's file did not change. When you add a file the app writes,
    ask where it lands when the settings are the test's — before the desk answers.

## Non-negotiable invariants
1. `Settings.live_enabled` is the ONLY path to real orders, and requires
   `TC_EXECUTION_MODE=mcp` **and** `TC_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`. Never widen this.
2. Every strategy signal must clear `cost_model.hurdle_bps()` or be recorded as rejected with a
   reason. Rejections are data, not failures.
3. A fitted coefficient whose confidence interval includes zero is forced to zero. No edge
   claimed means no trade placed.
4. Position sizing uses the LOWER bound of the edge interval, times `TC_KELLY_FRACTION` (hard
   capped at 0.5 in `config.py`).
5. Backtests execute one bar AFTER the signal bar, apply cost on both sides, and drop short
   signals (Robinhood does not permit shorting crypto) while counting them.
6. Every displayed statistic needs a `ModelCard` in `core/registry.py`.
7. Cost is PER COIN (`execution/symbol_cost.py`), never one global constant. The operator's
   0.245->0.2475 anecdote is the prior for the global base only; per-coin numbers come from
   observables and shrink toward real fills. Status labels assumed/ballpark/blended/measured
   must reach the UI unchanged.
8. Any pattern discovered by searching many buckets (calendar effects, lead-lag pairs) must
   pass Benjamini-Hochberg before it can filter or trigger a trade. No folk pattern gets
   hard-coded.
9. Advisory mode is a state machine in `execution/desk.py`: a ticket is not a trade, a
   position exists only after a reported fill, and only one close ticket may be outstanding
   per position.
10. `run.sh` must pass `bash -n run.sh`. It is bash, not zsh, and it must never prefix a
    brace group with environment assignments — that is a syntax error that stops the whole
    script from parsing, which is how the app silently failed to start. `./run.sh --check`
    diagnoses the environment without installing anything.
11. The trading day is midnight to 23:59 America/Chicago everywhere (ledger, budget, daily
    P&L), never UTC.
12. Per-strategy books are separate. Never present a combined equity curve as the headline —
    it hides one strategy paying for another's losses.
13. A trade budget is a CAP, never a quota. The threshold decays toward the quality floor,
    never below it, so an expiring slot can never force a trade that fails its cost hurdle.
    Asking for 10 and getting 3 is correct behaviour.
14. The false-breakout veto may only block when its bootstrap 95% CI on out-of-sample AUC is
    entirely above 0.5. An unproven model blocks nothing — see `breakout.veto()`.
15. `research/breakout.py` features must never read data after the break bar. `test_breakout.py`
    proves this by scrambling all future bars and asserting the features are unchanged. If you
    add a feature, extend that test.
16. `library.py`: a paper is only `applied` if a specific line of code implements it, and the
    entry must name the file.
17. Execution mode is RUNTIME state in `core/mode.py` (table `app_state`), not `.env` alone.
    Read it via `mode.get_mode()`, never `settings.execution_mode`. Paper and advisory are
    freely switchable from the UI; `mcp` requires `TC_LIVE_CONFIRM` in .env AND a restart, and
    `get_mode()` downgrades a stale live override to paper if that string disappears. A UI
    toggle must never be able to risk money on its own.
18. The traded universe is decided by `data/selection.py`, never a hand-written list. Roles:
    core (always tracked, majors set breadth and the leader reading), tradeable, watch
    (tracked for context, never traded — the FIL case), excluded. Promotion needs
    `enter_score`; demotion needs `exit_score` (lower) sustained for `exit_patience`
    reviews. A coin with no role starts at **watch** and must earn tradeable.
19. The engine builds its panel from ALL tracked coins but may only open positions in
    core/tradeable ones. Breadth computed on a shrunken universe is not breadth.
20. Cost hurdles are PER COIN (`symbol_cost.estimate_symbol`), never the global prior.
    Judging a BTC signal against 240bps when BTC's own round trip is ~80bps rejects
    viable trades.
21. Account equity is runtime state (`mode.get_equity()`), not `settings.account_equity`.
    Nothing in this app can read a real broker balance — it is a number the user enters.
22. Research runs itself via `core/scheduler.py`. Jobs are sequential, back off on failure,
    and record status/duration/summary. Add work as a job, not as a button.
23. Any module-level cache MUST be bounded. An unbounded `_LEVEL_CACHE` in `breakout.py`
    took the process down overnight — `veto()` wrote to it every tick and nothing cleared
    it. `test_health.py` asserts the bound; extend it if you add a cache.
24. The live loop reads a SHORT panel (`LIVE_PANEL_BARS`, cached `PANEL_TTL_S`) and cached
    cost tables. Never call `build_panel()` or `symbol_cost.observables()` per tick.
25. Research runs on HOURLY bars (`scheduler.RESEARCH_GRANULARITY`), never 1-minute.
    There are four years of hourly and only weeks of minute data, and minute data cannot be
    backfilled — running the walk-forward on it is why `breakout_train` reported
    "not enough events: 0".
26. The process stops itself at `TC_MEMORY_CEILING_MB` with exit code 3, which
    `scripts/supervise.sh` treats as a planned restart. Do not reuse exit code 3.
27. Retention is in `core/housekeeping.py`. Hourly and daily bars are never pruned.
28. UI density: explanation belongs in a hover `<Info text="…" />`, not a paragraph. Safety text
    (mode banner, verdicts, kill switch, news alerts) stays visible. `GET /setup` drives the
    Overview checklist — extend it rather than adding prose telling the user what to do.

29. The live mid is Robinhood's when Robinhood answers (`engine.prefetch_quotes`, one
    batched v1 `best_bid_ask` per tick for the coins the tick needs), Coinbase is the
    cross-check, and BOTH rows go to `quotes`: the Coinbase row keeps `spread_bps`
    (venue tightness for `symbol_cost.observables`), the Robinhood row carries
    `spread_bps=NULL, source='robinhood'`. `price_basis` never reads a robinhood row as
    "our feed". Never collapse these into one row.
30. Research jobs build the panel at the STRATEGY'S bar size (`scheduler._research_panel_for`).
    pump_catch reads 15-minute bars; grading it on hourly bars grades a rule the desk does
    not run.
31. A "collector split" (bar/quote collection in a second process) is deliberately NOT
    built: it would need a second writer on the same SQLite file, which is the exact thing
    that destroyed the database on 2026-09-17 (rule 5, `dbguard`). What a restart costs
    is measured (8-9 s of quotes per planned restart; bars refetch themselves). Revisit
    only with a separate market-data database file and its own process.

## API
Base `http://127.0.0.1:8006/api/v1`, response envelope `{success, data, message, timestamp}`.
Interactive docs at `/docs`. Key routes: `/system/status`, `/system/safety`, `/engine/{start,stop,tick}`,
`/movers`, `/cost`, `/cost/breakdown`, `/cost/observation`, `/models`, `/strategies`,
`/allocations`, `/signals`, `/orders`, `/orders/{client_id}/fill`, `/trades`, `/performance`,
`/equity`, `/events`, `/risk/{status,kill,release}`,
`/research/{backtest,cost-sensitivity,walkforward}`.

## Database
SQLite at `data/tradecrypto.sqlite`. Tables: `bars`, `quotes`, `universe`, `signals`, `orders`,
`trades`, `cost_observations`, `positions`, `equity_curve`, `model_cards`, `strategy_state`,
`runs`, `events`. The dashboard reads only from here — never re-derive state in the frontend.

## Testing
```bash
./run.sh --selftest          # feeds, db, models, statistics
cd backend && .venv/bin/python -m pytest tests/ -q          # on the Mac: the live file, read-only
# anywhere else: the newest ledger snapshot, never the live file (rule 5)
cd backend && TC_DB_PATH=$(ls -1t ../data/ledger/ledger-*.sqlite | head -1) python -m pytest tests/ -q
```
The suite never writes the desk: database read-only (`TC_DB_READONLY`), vault and kill
switch forced into a temp directory (`TC_VAULT_DIR`, `TC_KILL_SWITCH_FILE`), and the
session guard in `tests/conftest.py` refuses to start otherwise.
Statistical functions have property tests: deflated Sharpe must reject pure noise, PBO must sit
near 0.5 on noise, and the sizing function must return 0 when the edge CI includes zero. If a
change breaks those, the change is wrong.

## Data availability (decided by the venue, not by us)

| Resolution | How far back | Size, 80 coins |
|---|---|---|
| 1 day | to each coin's listing (~10y) | 15 MB / 4y |
| 1 hour | several years | 364 MB / 4y |
| 15 min | typically 1-2 years | 1.5 GB / 4y |
| 1 min | **weeks only — cannot be backfilled** | 5.5 GB / year, forward only |

Figures use this database's measured bytes-per-row. Collect 1-minute going forward for the
~20 coins actually traded, not all 80.

## Known gaps (do not paper over these)
- `RobinhoodMCPBroker.place()` deliberately raises. Headless OAuth against Robinhood's MCP is
  unproven; `advisory` mode is the working bridge.
- `universe.SEED_RH_SYMBOLS` is a guess until the MCP confirms symbols (`rh_confirmed`).
- No historical order flow is persisted, so no strategy has a validated edge yet. Every card
  says `exploratory` for a reason.
- PBO is unreliable below ~8 near-independent configurations; the walk-forward reports that
  rather than quoting a misleading number.
- The per-coin cost coefficients are documented priors until real fills exist on >= 6 coins.
  `fit_coefficients()["fitted"]` is False until then and the UI must say so.
- `flow_proxy` is a volume/impact proxy, NOT order flow. Robinhood exposes none. It cannot be
  backtested against ground truth and must stay labelled exploratory.
- No strategy has been calibrated on real Robinhood-tradeable history yet; all four report
  "no edge claimed", which is why nothing trades.
