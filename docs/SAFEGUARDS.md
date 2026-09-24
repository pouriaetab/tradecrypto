# Safeguards — index

The full blueprint ("Blueprint — safeguards for an automated trading application") lives in
the project workspace and is the shareable version: every entry is a real incident, the
mechanism behind it, the safeguard, and the test that proves the safeguard works. This file
is the in-repo index so the code and the reasoning stay together.

## Where each safeguard lives in this repo

| # | Safeguard | Implemented in |
|---|---|---|
| 0.1 | Log facts scoped to the current run | `scripts/facts.sh`, gate rule R1 |
| 0.3 | One command for "what is true right now" | `scripts/facts.sh` |
| 1.1 | Exactly one writer, enforced across the mount | `app/core/dbguard.py` |
| 1.2 | Read-only means immutable | `app/core/db.py::readonly_mode`, `tests/conftest.py` |
| 1.3 | Corruption is a repair, not a crash loop | `app/core/dbrecover.py::preflight` |
| 1.4 | Corrupt vs merely unopenable | `app/core/dbrecover.py::_is_corruption` |
| 1.5 | Irreplaceable rows snapshotted every 10 min | `housekeeping.ledger_snapshot`, job `ledger` |
| 1.6 | Ids survive a restore | `setup_ops.enforce_trade_id_high_water_mark`, gate rule R8 |
| 1.7 | Restores resurrect closed positions | `setup_ops.restore_lost_trades_once` |
| 1.9 | Raw-page salvage from a headerless file | `scripts/salvage_trades.py` |
| 2.1 | Sentinels are absence | `strategy/base.py::target_price`, invariant `targets_are_plausible` |
| 2.2 | Desk-wide denominators | `feedback/sizing.py` |
| 2.3 | Caps re-checked, not admission-only | invariant `no_position_risks_more_than_the_day` |
| 2.4 | Costs decompose | `execution/rh_spread.py::_delay_components` |
| 2.6 | Exit defined before entry | `research/exit_lab.py` |
| 3.1 | Proxy header decides the caller | `app/core/access.py::_forwarded_client` |
| 3.2 | 401 and 403 are different doors | `access.py::guard`, `frontend/src/App.jsx::TokenGate` |
| 4.1 | Outcomes reach their inputs | invariant `trades_link_to_orders` |
| 4.2 | Exclusions counted out loud | `research/invariants.py::_salvaged_ids` |
| 5.1 | A gate that runs | `scripts/gate.sh` |
| 5.3 | Fixes reach production | `scheduler._seed`, `scheduler_jobs.code_default_s` |
| 5.4 | Caches cannot hide the truth | gate rule R6, `PYTHONPYCACHEPREFIX` |
| 5.5 | Every route answers in the envelope | gate rule `rule_envelope` |
| 5.6 | A classifier fix reaches the rows it already classified | `data/news.py::retag_stored`, `MATCHER_VERSION` |
| 5.7 | A lab rule added later is back-filled on every trade | `research/exit_lab.py::absorb` (done = every rule present) |
| 5.8 | The lab's baseline is what was booked, not a rule declared live | `exit_lab.standings` row `as_traded` |
| 5.9 | Gates read the order the desk places, not a typed figure | `data/selection.py::_order_size_usd`, `_impact_gate` |
| 5.10 | Where a tick's time goes is measured | `engine.tick_profile`, `/system/status.tick_profile` |
| 5.11 | No git write on a mount from the Linux side | `CLAUDE.md` rule 6b, gate MANUAL R6b |
| 5.12 | The live mid is the venue's, batched; Coinbase stays the cross-check and keeps its row | `engine.prefetch_quotes`, `live_quote`; `price_basis` excludes source='robinhood' |
| 5.13 | A strategy's research runs on its own bar size | `scheduler._research_panel_for` |
| 5.14 | A faster clock is judged by replay, not by the anecdote that prompted it | `research/rolling_entry.py`, job `rolling_entry`, Strategies tab card |
| 5.15 | "Cut the bear day" is measured on every coin-day, not argued | `research/day_shape.py`, job `day_shape`, Daily tab card; lab rules `bear_cut_*` |
| 5.16 | The app's structure is read from the source, not drawn | `research/architecture.py`, `GET /architecture`, Data tab |
| 2.7 | The book is never positioned to lose more than the day has left | `guards.pre_trade_check` `book_risk_ceiling`; Risk tab "Book at risk" |
| 2.8 | One position per coin PER STRATEGY; stacking is recorded, bounded by 2.7 | `guards.pre_trade_check` `no_double_entry` / `stacked_position` |
| 3.9 | Every trade carries the rule version that made it; old versions are discounted by similarity, removed ones count zero | `research/versions.py`, `feedback/loop.py` weighted posterior, `relearn.score` |
| 3.10 | Regimes are found, not decreed; the router changes size only past 2σ and never past ×0.5/×1.5 | `research/regime_days.py`, jobs `regime_days` / `regime_today` |
| 3.11 | A lateness or entry-quality claim is a measured report before it is a rule | `research/entry_lateness.py`, `research/entry_quality.py` |
| 1.10 | No heavy read of the live file from the VM, ever | `CLAUDE.md` rule 5 (third incident, 2026-09-20 16:14) |
| 1.11 | No process on the VM opens the live file in ANY mode; our own tools read the newest ledger snapshot | `scripts/facts.sh` (claim-file check), `scripts/gate.sh` rule_liveness + rule_tests (`TC_DB_PATH`), `tests/conftest.py::skip_without` — fourth incident 2026-09-21 09:08 |
| 2.9 | The book's risk ceiling is the drawdown budget, not the day's cap: raise the bar, never ration solid signals | `guards.pre_trade_check` `book_risk_ceiling` (10% of equity less today's realised loss); `tests/test_book_risk_ceiling.py` |
| 2.10 | A test that trips a guard trips ITS switch, never the desk's | `TC_KILL_SWITCH_FILE`, `tests/conftest.py` (forced to temp + session guard), `tests/test_kill_switch_isolation.py` — 2026-09-21 08:37 |
| 3.12 | A coin's cost gate is its own typical day, not a fixed ceiling | `data/selection.py` cost gate (`round trip <= daily_range_pct`); 26 coins admitted 2026-09-21 that a 2.00% line refused, incl. XTZ/XPL/BONK/OP/WIF that were the week's 'no strategy watching' movers |
| 3.13 | A burst is caught in its first hour with a small target and a hard clock, as a paper experiment | `strategy/burst_catch.py`, `tests/test_burst_catch.py`; expected edge 0 until the lab says otherwise |
| 2.11 | A release while the day's loss is past the cap is the operator's decision for that day: the cap is waived until midnight and the switch does not re-arm for it; drawdown and per-order guards still apply | `guards.release_kill_switch` (`app_state.daily_loss_cap_waived_for_day`), `daily_loss_cap_waived_today`, Risk/Overview banners; `tests/test_daily_loss_waiver.py` — 2026-09-23, released nine times in four hours |
| 5.23 | A `.env` edit is a code change: it is inert until a restart, so autoapply treats it as one | `code_version._newest` watches `.env`; `tests/test_autoapply.py::test_an_env_edit_counts_as_new_code` — 2026-09-23, cap raised at 16:51, still $60 at 18:04 |
| 5.20 | A polled route never full-scans; expensive informational answers are shared for minutes | `db.query_one_cached` (`/system/status`, `/setup`, `retrain._data_days`), `daily_report.TODAY_REBUILD_S`, `symbol_cost` memo, `/coverage/today` memo; `tests/test_polled_routes_share_scans.py` — 2026-09-21 10:10 |
| 5.21 | Who holds the one database lock is readable, with every thread's stack | `db.LOCK_HOLDER`/`LOCK_WAITS`, `GET /system/threads`, Data tab "Who holds the database lock"; banner says slow vs down (`api.js offlineReason`) |
| 5.22 | The gate's database-open checker reads Python heredocs inside shell scripts too | `scripts/check_db_opens.py::_python_sources` — re-broken with facts.sh's own 09:08 open, caught |
| 3.14 | Prediction-market odds are context for the regime read, never an entry input | `data/prediction_markets.py` (Polymarket Gamma, hourly job `prediction_markets`), `GET /prediction-markets`, Universe tab "What the crowd expects"; `tests/test_prediction_markets.py` |
| 1.12 | Ledger snapshots keep a shorter window on a tight disk, never under six | `housekeeping.ledger_snapshot` (`_retention_scale` × `LEDGER_KEEP`), `tests/test_db_safety.py::test_ledger_window_shrinks_with_the_disk` |

## Before any change

    ./scripts/gate.sh          # add --quick to skip the suite

## To diagnose anything

    ./scripts/facts.sh         # boot | phone | db | jobs | all

## Adding a safeguard

When an incident repeats: add the entry to the blueprint (keep the incident — a rule
without its story gets deleted as pedantry), add a `rule_<id>()` to `scripts/gate.sh` if it
can be checked, add the test, and **prove the test by re-breaking the mechanism**.

## Reading the live database safely — and what it costs

`?mode=ro&immutable=1` is the only safe way to read the live file from anywhere that is
not the app: it skips the WAL and the shared `-shm` index entirely, so it maps nothing and
writes nothing.

The cost is that it **also skips everything in the WAL** — every write since the last
checkpoint is invisible. A brand-new table can look as though its migration never ran.

So: use immutable when the question is "is this file intact"; use the newest
`data/ledger/*.sqlite` snapshot when the question is "what is true right now".

## Notes behind blueprint v1.3 (now folded in: §3.6, §5.7, §5.8)

**An always-on policy needs an off switch that every starter honours.**
`KeepAlive` alone means the desk can never be stopped — Control Deck's Stop is undone in
seconds and there is no way to hold it down while you work. The authority is a file, not a
button: `data/STOP_SUPERVISOR`. launchd's `KeepAlive/PathState` will not revive the desk
while it exists, and `supervise.sh` already refuses to start against it. One file, two
enforcers — a pause only one of them honours is not a pause. `scripts/desk.sh on|off|status`
is the switch; `facts.sh` reports the paused state so "it will not start" never looks like
"it is broken".

Note the `KeepAlive` subtlety: a dictionary is **OR'd** across its conditions, so `PathState`
must be the *only* condition. Adding `SuccessfulExit` beside it would revive the desk after
a crash even while paused.

**Never exercise a production switch on production.**
While testing the above, `desk.sh off` was run against the live desk — writing the real stop
file that blocks it from starting. The switch now takes a `DESK_ROOT` override so it can be
exercised against a scratch tree. If a control has an off position, the way you verify it
must not be to turn the real thing off.

**A checker must find its dependencies the way the project installs them (extends 5.7).**
`render-check` looked only in `node_modules/.bin`. This project uses pnpm, where `.bin` holds
only direct dependencies and the real binary lives under `node_modules/.pnpm/@esbuild+<platform>@<ver>/`.
It failed on the machine that owns the repo while passing anywhere `ESBUILD_BIN` happened to
be exported. Two lessons: resolve through the project's actual layout, and **match the
platform** — a macOS binary found from Linux runs far enough to fail with "bundling failed",
which reads like a broken component rather than the wrong executable.

## A restart nobody has to remember (2026-09-18)

**The failure.** Python loads its modules once, so every backend change sat inert until
someone remembered to stop and start. The operator's working loop was edit → remember →
stop → start → hope. His words: *"my ideal scenario is to never close or open stop or
restart."*

**Two pieces already existed and were never connected.** `code_version` knows the source on
disk is newer than the running build (written after this app spent four days executing a
three-day-old build while every screen looked normal). `supervise.sh` already treats **exit
code 3** as a *planned* restart — no backoff, no failed-start counter.

**The safeguard.** `autoapply.check()` runs between engine ticks and exits 3 once the code is
stale **and** it is safe. Each condition earns its place:
- **No order in flight.** A restart between "submitted" and "filled" is how you lose track of
  real money. This is the one that would cost.
- **Edits have settled** (90s). A file saved 4 seconds ago is probably one of several; a
  restart mid-edit runs half a change.
- **The process has been up a while** (120s), so a clock skew cannot loop the desk at boot.

Self-limiting by construction: after the restart the running build *is* the newest build, so
the condition clears itself. No cooldown state to keep.

**Proven by re-breaking:** removing the in-flight check and removing the settle delay each
turn their test red.

## Boot must not duplicate the scheduler (2026-09-18)

**The failure.** "The app takes forever to load." Boot ran `universe_review`, `hour_profile`
and `daily_report` unconditionally every single time — jobs the scheduler already owns on
daily and 6-hourly cadences — plus up to twelve history passes. On a ten-second restart to
pick up a code change, that was minutes of API calls redoing work finished minutes earlier.

**The safeguard.** `scheduler.due(name)` answers "is this actually overdue?", and boot runs
each catch-up job only if it is, logging the skip. When it cannot tell, it runs the job —
never skip silently.

**And measure it.** A planned restart stamps `planned_shutdown_ts`; the next boot logs the
real downtime and journals it. "Limit the downtime" is only a goal if it is measured.

## A config file you generate is code — parse it before you ship it (2026-09-18)

**The failure.** The LaunchAgent plist carried an explanatory XML comment. One sentence
contained a double hyphen (`… authoritative -- adding SuccessfulExit …`), which is illegal
inside an XML comment, so the entire plist stopped parsing. `launchctl` reported only:

    Load failed: 5: Input/output error

That message is indistinguishable from a permissions problem. It very nearly cost a grant of
**Full Disk Access to /bin/bash** — a broad, hard-to-undo security change — to fix a typo.

**The safeguards.**
1. **Validate before installing.** `plutil -lint` runs before `launchctl bootstrap`, and the
   installer refuses rather than writing a file it has not parsed.
2. **Report the real error.** A failed load prints launchctl's actual stderr and the file
   path instead of being swallowed by `2>/dev/null`.
3. **Keep the essay out of the generated file.** Reasoning belongs in the script that writes
   the config, where no parser can choke on it. The plist keeps one short line.
4. **Test it.** `tests/test_launch_agent.py` renders the heredoc and parses it — both the
   default and `--awake` variants — asserts `KeepAlive` has `PathState` and nothing else
   (the dictionary is OR'd, so a second condition would revive a paused desk), and asserts
   no XML comment contains `--`. Proven by re-breaking: 6 of 7 go red.

**The general rule.** When a tool's error points at a cause you cannot verify, verify the
thing you just generated first. The error message is evidence about the tool, not about the
world.

## The shared cross-project checklist (2026-09-18)

These lessons have a second home: `../webapp_blueprint/CHECKLIST.md`, which spans every app
in this workshop. **Both get fed, in the same session, by whoever hit the problem** — see
`CLAUDE.md` rule 0 (read it first) and rule 8 (feed it after). A lesson that stays in this
repo only protects this repo.

Proof that this matters: on 2026-09-18 this project independently rediscovered two rules that
were already written in the shared file, and then imported them —

- **1.6 — `kill -0` succeeds on a zombie.** `dbguard._pid_alive()` used a bare `os.kill(pid, 0)`,
  so a crashed desk would have kept its claim on the database and the next start would have
  been refused for up to 90 seconds for no reason. Now checks `ps -o stat=` and treats `Z` as
  dead, while an unreadable process table still never declares a live pid dead.
- **4.20 — WAL still fsyncs on every commit** unless `synchronous=NORMAL`. We had
  `journal_mode=WAL` and the default `synchronous=FULL`, i.e. an fsync per commit on a desk
  that commits every tick. Measured on a sibling project at 4.99 ms median / 34.7 ms worst on
  a much smaller file than ours. `NORMAL` under WAL cannot corrupt the file on a process
  crash; the only exposure is losing the last transactions to a power cut, and we can afford
  exactly that because every ownership-changing event is also fsync'd to `core/journal.py`.

Both proven by re-breaking. 21 rows went the other way, into the shared checklist.

## The heartbeat was eating its own evidence (2026-09-18)

`dbguard.beat()` rebuilt the whole claim every 20 s, so `since` was reset to now on every
beat and the claim file always read as brand new. A healthy desk that had been up 25 minutes
looked like it had just restarted — and it misled the author of the code, in this session,
while diagnosing something else. A heartbeat says "still here", not "just arrived": it now
carries the original `since` forward. (The staleness test uses `heartbeat`, so this was a
diagnostic bug, not a safety one — which is exactly why it survived.)

Also fixed in the same pass: the guard's refusal message told the next person to
`sqlite3.connect('file:…?mode=ro')`, the advice that caused the 08:44 burst of
`file is not a database`. It now says `immutable=1` and says why. A rule that recommends the
dangerous option is worse than no rule, and the test that pinned the message was rewritten to
assert the *safe* advice rather than merely that the message mentions `mode=ro`.

## One exit code, one meaning (2026-09-18)

Auto-apply reused exit code 3 — the supervisor's memory-ceiling code — because the handling
was identical (restart, no backoff). So the supervisor logged **"memory-ceiling restart"**
every time the desk had merely picked up an edit, and a real memory-ceiling restart would
have been invisible among them. Auto-apply now exits **6** and the supervisor says
"restarted to pick up new code". Identical handling is not a reason to share a code: the code
is how the log tells the truth about *why*.

The test for this is worth noting too. The first version grepped the case-6 block for the
word "memory" — and failed on the comment explaining why "memory" must not appear there.
Assert on what a handler **says**, not on its comments.

## The gate learned to parse instead of grep (2026-09-18)

The rule "nothing outside the app's database layer opens the live file unsafely" was a grep.
It flagged two *explanatory strings inside an error message* and two opens of
`tradecrypto.recovered.sqlite` — a different file whose name only appears on the previous
line — while a real offender hidden behind a variable would have passed. It is now
`scripts/check_db_opens.py`, which walks the AST, resolves the connect target through local
assignments, and judges by the file. Verified against a planted tree of two offenders and
three innocents (a different file, an `immutable=1` open, and a connect string quoted inside
a message): 2 caught, 3 spared, and the real tree clean.

If a rule is about code structure, parse the code. A gate that cries wolf gets switched off.

## The agent that was not running is the whole safety net (2026-09-18)

At 11:56 local the desk was killed by SIGTERM — `exit 143`, 45 seconds after a planned
auto-apply restart — because its launcher quit and took it along. It stayed down for
**21 minutes** and only came back when Start was pressed. The launchd agent that exists
precisely to catch that was failing every 10 seconds on the TCC permission error, and had
been all day.

"Autostart is installed" and "autostart runs" are different facts. Until that agent loads,
there is no safety net — only the appearance of one — and every stop of the desk is silent
and open-ended. This is a P1, not a to-do.

## The vault (2026-09-19)

The operator's requirement, after losing eighteen hours of trades: *"how do you
assure me that we will never miss any of the orders data, i need this to be 100%
and even have a backup vault that should never be needed to access and only can
add data to it as a redundant place but independent completely from any other
parts of the whole system."*

`core/journal.py` was the second copy, and it was not enough: it shares the
project folder, the process and the machine with the database it exists to
outlive. `core/vault.py` is the third, and it shares none of them.

**What independence means here, checked by tests rather than claimed:**

1. **Outside the project.** `~/tradecrypto-vault` by default, `TC_VAULT_DIR` to
   override. `status()["inside_project"]` is asserted false, and the Overview
   row turns red if it ever becomes true.
2. **Not a database.** One JSON object per line. `cat` and `grep` read it.
3. **Not dependent on this codebase.** A test asserts `record()` contains no
   import from `app.*`, so nothing anyone changes elsewhere can break the one
   write that must not break. `scripts/vault_verify.py` reads the vault with a
   stock python3 and no project on the path at all.
4. **Append-only by construction and by permission.** Files open in `'a'`; a day
   that is over is `chmod 444`. Nothing in the app ever reads the records —
   `reconcile()` reads only the small index of keys.
5. **Durable.** `fsync` on the record *and* on the index, both asserted by a
   test — which exists because the first re-break removed the fsync and every
   other test stayed green.
6. **Tamper-evident.** SHA-256 chained per line. Editing a value is caught at
   that line; removing one is caught at the line after; a tail truncated by a
   crash still verifies up to the cut and is reported as a crash, not as
   tampering, because losing new data to protect old data is a bad trade.
7. **Off this machine.** `scripts/vault_mirror.sh` commits and pushes to a
   private git remote, hourly, and **never force-pushes** — a test asserts that,
   because a force push is the one command that can erase a write-once store.

**Why it is 100% and not best-effort.** Two mechanisms, because one never is:

- `record()` runs synchronously inside the same call that places the order or
  closes the trade. No queue to drain, no worker to die.
- `reconcile()` compares the vault's key index against the database and appends
  whatever is missing — at boot and every ten minutes. So a write that *did*
  fail (full disk, revoked permission) is caught instead of becoming a silent
  hole.

The first is the promise. The second is what makes it checkable, and the
difference between those two is the whole lesson of this week. On its first pass
it appended 90 records; on every pass since, 0 — which is what a working one
looks like.

## Four trades had no verdict, and nothing was ever going to fix them (2026-09-19)

`reattribute_trades_once()` did its job and marked itself done — *before* the
trades salvaged out of the corrupt database were inserted. So #32, #36, #37 and
#38 sat in the Journal showing "—" where the verdict goes, and the only thing
that would have labelled them had already retired.

Three changes:

1. `attribute_trade()` now **merges** into the stored attribution instead of
   replacing it, so `provenance` — the only record that a row was salvaged —
   survives being re-labelled.
2. A recomputed verdict says so: `verdict_recomputed_at` plus a note that the
   label written at close time was lost and this one is derived from the numbers
   that were recovered. An honest gap beats a gap made to look ordinary.
3. `label_unlabelled_trades()` is **not a one-shot**. It scans for the condition,
   not a flag, and runs every boot. A test asserts it is not gated by
   `app_state`, because that gate is the whole reason this happened.

## Two leaks that made the gate fail by running it (2026-09-19)

The render check created a ~2 MB temp directory per run and never removed it.
Ninety-six of them took the temp volume under the floor the gate itself checks
for, so the gate failed with *"the test suite cannot create temp dirs"* — which
reads like a broken suite. It now cleans up on exit, on SIGINT and SIGTERM, and
sweeps what earlier runs left. The test suite's throwaway vault does the same.

A check that fails the gate by running is a check that gets switched off.

## The test that passed alone and failed in the suite (2026-09-19)

A vault test stubbed `record()` to fail, then called `monkeypatch.undo()`.
`undo()` reverts **every** patch on that test function — including the ones the
*fixtures* set — so it also cleared `TC_VAULT_DIR` and pointed the rest of the
test at the operator's real, append-only vault, which by design cannot have
anything taken back out of it.

It passed alone (an earlier test had left the env var set) and failed in the
suite. Order-dependence is never the bug; it is the symptom of one. Now: restore
by hand in a `finally`, and `conftest.py` forces `TC_VAULT_DIR` to a throwaway
directory for the whole session, where no test can undo it.

## Two blank cards and a banner that was a pill (2026-09-19)

`/liveness` and `/vault` returned their dicts bare, without `ok()`. The
frontend unwraps `.data` from every response, so both cards rendered their
empty state — *"the mechanism registry has not reported yet"* over a registry
that had counted 84 orders — with no error anywhere. Found by reading the JSON
in the browser, not by any checker. Gate rule `rule_envelope` now walks every
route for a `return` that skips the envelope.

On the same page the info banner was a 13-px nowrap pill: the icon rule `.info`
also matched `.banner.info`. Scoped to `span.info`.

## The exit lab compared everything to a rule that was not running (2026-09-19)

`trail_8_breakeven` was labelled "running today" and every verdict was measured
against it, at -0.96%/trade. Only `pump_ride` sets `trail_bps`; every open
position that day belonged to `volume_build` or `day_climb`, which use a fixed
target, a catastrophe stop and a time limit, and never enter the ratchet — the
liveness card already said so (`stop_ratchet: known not live`). The desk's real
result on the same 39 trades was **+1.12%/trade**, better than every lab rule.
The baseline is now an `as_traded` row computed from the trades table.

Two more in the same file: `absorb()` keyed "done" on trade_id, so a rule added
later was replayed on the 8 trades after it and never on the 31 before, and sat
at n=8 forever; and `hold_to_close` inherited the breakeven floor and exited 29
of 39 trades flat while calling itself a hold.

## A liquidity floor that never read the book (2026-09-19)

"Our $100 order must be a rounding error in $2M/day" — both numbers typed in.
The book was $2,000 and the largest open $252. Replaced by square-root impact
from the p90 of recent opens against hourly dollar volume, tolerated up to the
coin's own cost error bar capped at its one-side spread. On the live universe:
41 coins the fixed floor refused, none it admitted that the new gate refuses,
worst impact 18 bps against a 95 bps tolerance. Tests re-broken (impact set to
0 → 2 of 6 red).

## The same rule on a faster clock, and what the replay said (2026-09-19, evening)

PENGU was bought at 12:24:05 at 0.008301 — the top of a run that began at
11:39. `volume_build` reads the forming calendar hour on hourly bars refreshed
every ten minutes; every open over three days had landed 1–57 minutes after
its hourly bar. `pump_catch` is the identical rule on a rolling 60-minute
window of 15-minute bars (parity proven in `tests/test_pump_catch.py`).
`research/rolling_entry.py` replayed both clocks over the same 190 days and 30
coins with the same exit: on the 47 moves both caught, the rolling clock was a
median 30 minutes earlier, 0.31% lower, +0.81%/trade against +0.14%; it also
made 64 entries the hour never made at −0.80%/trade. Level overall. Earlier,
yes; without more false entries, no. It runs in paper beside volume_build; the
job re-decides daily.

Two mechanisms that came with it: research jobs now build the panel at the
strategy's own bar size (a 15-minute strategy walked forward on hourly bars
would have graded a rule the desk never runs), and candle refreshes fetch three
at a time (HTTP only in threads; SQLite from the tick thread alone).

## The bear day, measured on 32,000 coin-days (2026-09-19, evening)

"Cut the losses sooner when the day is bear" — `research/day_shape.py` reads
every coin-day at 06/09/12 local: below the open, lower highs, lower lows.
Bear days at 09:00 finish the day −0.05% against +0.20% for the rest (−5.6σ):
real, and smaller than one side of the spread. At 06:00 they recover as often
as not. So the shape is information about the DAY, not a reason to sell into
it; the exit lab's `bear_cut_*` rules replay the same test on the desk's own
trades so the two views can be checked against each other.

## The burst, the ceiling, and a bus error (2026-09-20)

At 12:00 nine day_climb signals fired in one bar and were all funded — 19
positions, $1,244 deployed, $108.87 at risk against a $60 daily cap that only
counts realised losses. `book_risk_ceiling` now refuses an open when open risk
plus the order's risk exceeds the day's remaining loss headroom; signals arrive
in the strategy's rank order, so the best get funded and the rest carry the
reason. Tests re-broken (3 of 4 red with the check forced to pass).

At 16:14 an `immutable=1` read of the live file from the Linux VM (a 35,000-bar
panel for a research fit) ran across a planned restart. The VM saw `database
disk image is malformed`; the app died with `Bus error: 10` in its first tick;
the next boot read `[db] database ok`. The file was fine. Two coincidences now.
Heavy reads of the live file from the VM are not done at all any more —
research that needs the bars runs as a job on the Mac and is read from `runs`.

## 2026-09-21: two incidents from our own tooling, and what changed

- **08:37 — the desk's kill switch, engaged by a unit test.** `test_a_loss_already_taken_today_shrinks_the_headroom`
  built a $150 loss in its private temp database and called `pre_trade_check`. The guard did its
  job and wrote the switch at the hardcoded path `data/KILL_SWITCH` — the project's, through the
  mount. No position opened until the operator released it at ~09:40; the day's real realised P&L
  was +$7.46. The CRITICAL event went to the temp database, so the live log never said why. Fix:
  the path is a setting (`TC_KILL_SWITCH_FILE`), the suite forces it into its temp dir, the
  session guard refuses to run otherwise, and a test trips the cap on purpose and checks the
  desk's file did not change (blueprint 2.21).
- **09:08 — SIGBUS, from `facts.sh`.** Its "scheduled jobs" section opened the live file `mode=ro`
  from the VM: one SELECT over 28 rows. Twelve seconds later the backend died with `Bus error: 10`
  (946 s into a boot, mid catch-up). Fourth incident of the class; not the size of the read, the
  -shm mapping. `facts.sh`, `gate.sh` (liveness, test suite) now detect they are off the holding
  machine and read the newest `data/ledger/` snapshot. Tests needing `bars`/`quotes` skip there
  (`skip_without`) and run in full on the Mac. With the VM's shared disk at 0 MB the suite ran in
  the cloud container against a staged snapshot: 342 passed, 2 skipped, 34 s (blueprint 5.19).
- **Ceiling.** "The book already risks $59.99 … the day has $60" blocked 32 entries on 09-21
  morning. The daily cap is a stop on losses already taken; using it as the book's ceiling
  rationed good signals. The ceiling is now the drawdown budget (10% of equity = $200) less today's
  realised loss; one solid signal is never refused because eight others are already on. Yesterday's
  nineteen would all have been allowed (test).
- **Cost gate.** A fixed 2.00% round-trip line kept XTZ (2.12%), BONK (2.19%), XPL/ZORA (2.01%)
  out of the universe while they moved 8–32% — the week's "no strategy watching" list was mostly
  this. The gate is now "round trip ≤ the coin's own vol-implied daily range"; the 09:20 review
  admitted 26 coins the old line refused and refused 5.
- **Disk.** Three of four identical 440 MB corrupt copies in `data/quarantine` deleted (1.3 GB;
  the 05:03:54 original kept), the empty `_to_delete_tmp` removed, ledger window now scales with
  free space. The Mac reported 27.5 GB free afterwards (above the 15 GB line; retention normal).

## 2026-09-23 — "just let it trade": the release that would not stick

The paper desk was $71 (later $87, $97) down against a $60 daily cap. Between 14:22 and 18:04 the
operator released the kill switch nine times; each time the next signal re-read the same loss and
re-engaged it within minutes. At 16:51 he raised `TC_MAX_DAILY_LOSS_PCT` to 8 in `.env`; the running
process never saw it (settings are read at boot) and nothing said so. Two fixes: a release taken
while the loss is past the cap now waives that cap for the rest of the day — logged as the
operator's decision, expiring at midnight, drawdown and per-order guards untouched — and a `.env`
edit now triggers the same planned restart a code edit does. The 18:51 restart applied the 8%
cap ($160); realised −$94.76, headroom $65, switch off, engine trading. 549 tests pass.

## 2026-09-21 10:10 — "Backend not answering" for forty minutes while it was up

Measured, not guessed: `/mode` 27 s, `/health/report` (no database) 24 s, a 365-second tick,
no restart since 09:26. `GET /system/threads` — added for this — showed the one database lock
held by API threads running `COUNT(*)` and `MIN(ts)/MAX(ts)` over the 3-million-row `bars`
table (`/system/status`, `/setup`), `COUNT(DISTINCT date(ts))` (retrain status, seven times per
poll), and every minute bar of the week into Python dicts (`/movers` → cost observables, twice
per poll). Ninety seconds after a boot: 3,577 lock acquisitions, 354 s of cumulative waiting.
The engine's tick queued in the same line; the anyio threadpool (40) filled with waiters, so a
route that never touches the database queued behind them too. Fix: share those answers
(2 min for counts, 5 min for cost tables, today's report rebuilt at most every 2 min, coverage
1 min). After, same load: 6,513 acquisitions, 48 s waited, mean 7 ms, max 4.8 s; `/mode` 0.56 s,
`/system/status` 1.3 s, ticks 2–3 s. Tests pin each shared scan. The banner now says
"answering slowly" (a timeout) versus "not answering" (nothing listening), and the Data tab
shows who holds the lock. Blueprint 4.29, 5.20.

## What the historical fits said (2026-09-20)

- Regimes (Gaussian mixture, k=5 by BIC, 1,462 days): bear-narrow 341, bear-narrow-wild 193,
  bull-broad 397, bull-broad-wild 120, flat-quiet 411. day_climb is −2.35%/trade on
  bear-narrow days vs −1.71% overall (−5.0σ, n=2,060); volume_build −2.63% vs −1.43%
  (−2.8σ); morning_dip +2.4σ better on bull-broad-wild. Wild bear days are NOT worse —
  those are the bounce days. Router: day_climb ×0.5 on bear-narrow, morning_dip ×1.5 on
  bull-broad-wild, everything else ×1.0.
- Lateness: no strategy's outcome is predicted by how late it bought (day_climb ρ=+0.004
  over 13,114 entries). Today's "bought the top" was a bad hour, not a rule.
- Entry quality (L2 logistic, chronological 75/25): day_climb held-out AUC 0.515
  [0.495, 0.533], top-vs-bottom quintile +0.20% (0.7σ) — not usable. The winning climb is
  not linearly separable from the losing one on these features; day_climb's problem is
  cost (−1.9% net vs ~0 gross), not selection. oversold_turn 0.576 [0.483, 0.679], +3.9%
  (1.5σ) on 126 held-out — promising, thin.
