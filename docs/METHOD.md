# Method: the same work, in the language each field uses

## Why this document exists

There is a specific failure that costs people interviews. You do a piece of
work, it feels obvious while you are doing it, so you describe it plainly as
*"I checked the trades matched the signals"*, and the interviewer hears
housekeeping. Someone else does the identical thing and says *"I implemented
end-to-end traceability between decision records and execution records, with
automated invariant checks and a defined escalation path"*, and the interviewer
hears an engineer.

Both sentences describe the same code. Only one of them is recognised.

This document takes each part of this project and writes it three ways:

1. **The named process.** What the discipline calls it, with the current
   standard.
2. **The steps.** The canonical sequence, numbered, so it can be walked
   through out loud.
3. **What was actually done here**, with real numbers, so every step has a
   concrete example behind it.

Then it shows the **domain swap**: the same step with the subject changed from
market data to vehicles, aircraft, robots or a production line. The method does
not change. Only the nouns do.

> **Rule for using this:** every claim below is about *this project*. Where your
> employment history supports something, say so separately and keep the two
> apart. See §11 for the boundary.

---

## 1. Hazard analysis

### 1.1 The named process

Two live frames, and they answer different questions.

**HARA** is Hazard Analysis and Risk Assessment, the automotive frame from
ISO 26262:2018. Asks: *for each hazardous event, how bad, how often, how
avoidable?* Output is an integrity level and a safety goal.

**STPA** is Systems-Theoretic Process Analysis (Leveson & Thomas, *STPA
Handbook*, 2018). Asks: *what control action, in what context, would be unsafe?*
Built for software-intensive and autonomous systems where the hazard is not a
component breaking but a controller doing the wrong correct-looking thing. This
is the frame that fits a statistical decision pipeline, and it is the one worth
leading with for autonomy roles.

### 1.2 The steps: STPA

1. **Define losses and system-level hazards.** What outcome is unacceptable?
2. **Build the control structure.** Controllers, actuators, sensors, and the
   control actions between them.
3. **Identify Unsafe Control Actions (UCAs).** For each control action, four
   ways it can be unsafe: provided when it should not be; not provided when it
   should be; provided too early/late/out of order; stopped too soon or applied
   too long.
4. **Identify loss scenarios.** Why would that UCA occur? Bad process model,
   bad feedback, missing input?
5. **Derive requirements and constraints** that eliminate or control each
   scenario.
6. **Verify the constraints are implemented and effective.**

### 1.3 What this project did

| step | in this system |
|---|---|
| Losses | Unrecoverable capital loss; operator acting on a belief the system does not support |
| Control structure | Feed → strategy (controller) → gate chain (safety controller) → broker (actuator) → position state (feedback) |
| UCA: *provided when it should not be* | An order placed while the day's loss budget is exhausted → gate `daily_loss_cap` |
| UCA: *provided when it should not be* | An order in an instrument already held → gates `no_double_entry`, `coin_stacking` |
| UCA: *not provided when it should be* | An exit not taken as the odds decay → `dynamic_target` re-prices the exit as `time_budget` odds fall |
| UCA: *too late* | A stop that never ratchets → `liveness` asks whether that mechanism has **ever fired** |
| Loss scenario: bad process model | The controller believes 11 positions are 11 independent bets; measured correlation +0.43 gives **2.1** effective bets |
| Derived constraints | **17 named pre-trade gates**, each recording limit, reading and verdict |
| Verification | **15 data invariants** + **541 tests** |

The 17 gates: `kill_switch`, `daily_loss_cap`, `max_drawdown`,
`book_risk_ceiling`, `cash_available`, `max_position_usd`,
`max_concurrent_positions`, `max_trades_per_day`, `min_notional`,
`no_double_entry`, `coin_stacking`, `lone_candidate`, `shape_odds`,
`feed_cross_check`, `strategy_control`, `live_confirmation`,
`symbol_confirmed_on_robinhood`.

### 1.4 The domain swap

| here | autonomous driving | aerospace / UAS | manufacturing |
| --- | --- | --- | --- | --- |
| Order placed with budget exhausted | Lane change commanded with insufficient gap | Descent commanded below minimum safe altitude | Cycle started with guard door open |
| Exit not taken as odds decay | Braking not initiated as TTC decreases | Go-around not initiated as approach destabilises | Line not stopped as SPC drift grows |
| Correlated positions counted as independent | Redundant sensors sharing a common failure mode | Redundant channels on one power bus | Parallel lines sharing one calibration source |

**The sentence:** *"I used an STPA-style frame of control structure, unsafe
control actions and loss scenarios, and turned each unsafe control action into
an enforced pre-trade constraint. There are seventeen of them, each records its
limit and its reading, and a separate liveness check confirms whether each one
has ever actually fired."*

---

## 2. Risk management

### 2.1 The named process

**ISO 31000:2018**, *Risk management — Guidelines*. Note the title: the 2009
edition was *"Principles and guidelines"*, and using that phrasing marks a stale
citation. ISO 31000 is guidance and is **not certifiable**. Techniques live in
**IEC 31010:2019**, *Risk management — Risk assessment techniques*, which is
where bow-tie analysis is formally described.

### 2.2 The steps

1. **Scope, context, criteria.** What is in scope and what counts as
   acceptable.
2. **Risk identification.**
3. **Risk analysis** of likelihood and consequence.
4. **Risk evaluation** against the criteria, deciding what needs treating.
5. **Risk treatment** by avoiding, reducing, transferring or accepting.
6. **Monitoring and review.**
7. **Recording and reporting.**
8. Throughout: **communication and consultation.**

### 2.3 What this project did

1. **Context and criteria.** Declared capital; a maximum acceptable daily loss
   as a percentage of it, adjustable at runtime within a bounded range
   (0.5%–50%, values outside refused with a stated reason).
2. **Identification.** Cost exceeding edge; correlated exposure; stale prices;
   a runaway loop; a latched safeguard; a silent feature failure.
3. **Analysis.** Round-trip cost measured at **~1.90%** against a **2–3%**
   typical daily range. Correlation measured at **+0.43**, giving **2.1**
   effective independent bets from 11 positions.
4. **Evaluation.** Every strategy compared against its own cost hurdle; none
   clears it.
5. **Treatment.** 17 pre-trade gates; a day stop; a drawdown ceiling; a
   book-level risk ceiling; position sizing as a fraction of declared capital
   rather than a fixed sum.
6. **Monitoring.** Live exposure, headroom against the day's budget, and a
   liveness check on each safeguard.
7. **Recording.** An append-only, hash-chained audit log written outside the
   application directory and never read back by it.

### 2.4 The domain swap

Residual risk after treatment, monitored continuously, with a defined
escalation. That structure is identical whether the consequence is money, a
collision, a hull loss or a recall. **The registers differ; the process does
not.**

**The sentence:** *"I ran it as an ISO 31000-shaped loop: context and criteria
first, then identification, analysis against measured rather than assumed
numbers, treatment as enforced controls, and continuous monitoring with the
residual risk on screen. The criteria are operator-adjustable at runtime and
bounded, because a limit that requires a restart to change is one people work
around instead of changing."*

---

## 3. Verification and validation

### 3.1 The named process

**IEEE Std 1012-2024**, *IEEE Standard for System, Software, and Hardware
Verification and Validation*. Two cautions: it supersedes 1012-2016, and since
2016 the title covers system, software *and hardware*, so calling it "the
software V&V standard" dates you.

The distinction, stated the way it is usually asked:

- **Verification.** Are we building the thing right? Does the implementation
  meet the specification?
- **Validation.** Are we building the right thing? Does it meet the need in the
  operating environment?

**Integrity levels** drive how much V&V rigour is required.

### 3.2 The steps

1. Establish integrity levels.
2. Plan V&V activities against them.
3. Verify requirements, design, implementation.
4. Validate against operational need in the intended environment.
5. Maintain **bidirectional traceability**.
6. Report anomalies and manage them to closure.
7. Re-verify after change, which is regression.

### 3.3 What this project did

**Verification.** Does the code do what the design says?
- 541 automated tests across 58 files.
- 15 data invariants asserting structural properties, e.g.
  `trades_link_to_orders`, `orders_link_to_signals`,
  `trades_reach_their_features`, `pnl_decomposes`, `equity_reconciles`.
- An undefined-name gate over every language in the project, after a silent
  failure where a `NameError` inside a broad `try/except` meant a feature wrote
  nothing and nothing ever failed.

**Validation.** Does the thing meet the need?
- The need was a positive expected return after costs. It does not meet it.
  **That finding is the validation result**, and reporting it is the point.

**Traceability.** The chain is enforced rather than documented:
`signal → order → order → trade`, with features attached to the signal, so any
closed trade reaches the exact inputs that produced it. When that link broke,
every downstream learner was training on nothing while the system reported that
learning was enabled. It is now an invariant.

### 3.4 The domain swap

| here | elsewhere |
|---|---|
| Trade reaches its features | Every logged event reaches the sensor frame that triggered it |
| P&L decomposes | Measured output reconciles with its components |
| Prices on screen are fresh | Displayed telemetry is within its staleness budget |

**The sentence:** *"Verification was automated invariants and a test suite of
five hundred-odd tests, fifteen structural invariants, and enforced bidirectional
traceability from decision record to execution record. Validation was against
the operational need, and it failed: the system does not clear its own
transaction cost. I reported that rather than re-tuning until it looked better."*

---

## 4. Test engineering

### 4.1 The named process

**ISO/IEC/IEEE 29119**, *Software and systems engineering — Software testing*.
Five normative parts: **-1:2022** general concepts, **-2:2021** test processes,
**-3:2021** test documentation, **-4:2021** test techniques, **-5:2024**
keyword-driven testing. Plus **ISO/IEC TR 29119-11:2020**, *Testing of AI-based
systems*, which is directly on point and few candidates have heard of it.

### 4.2 The steps

1. **Test strategy**, meaning risk-based prioritisation.
2. **Test planning** across levels: unit, integration, system, acceptance.
3. **Test design** using techniques: equivalence partitioning, boundary value
   analysis, decision tables, state transition, pairwise.
4. **Test oracles.** How you know the answer is right. The hard part for any
   statistical system.
5. **Adequacy criteria / coverage.**
6. **Execution, incident reporting, regression.**

### 4.3 What this project did: the oracle problem, worked

A statistical system has no ground-truth oracle: you cannot assert the model
"should" have produced 0.6. So the oracles are **properties**, not values:

| oracle type | example here |
|---|---|
| **Metamorphic** | Halving the size multiplier must halve the notional; this caught a cap binding asymmetrically |
| **Invariant** | Every closed trade reaches its features |
| **Statistical control** | A model with no variables must score AUC 0.500 |
| **Regression on a known incident** | Each of the failure-log entries has a test that reproduces it |
| **Boundary value** | The day-stop accepts 0.5% and 50%, refuses 0.0% and 80% with a stated reason |
| **Temporal** | Splits must be by time; a check asserts no shuffle call exists in the split path |

**Worked boundary example.** Day stop as a percentage of declared capital:

- Partitions: below minimum / valid / above maximum
- Boundaries: 0.4, **0.5**, 50, 50.1
- Expected: refusal with a reason / accept / accept / refusal with a reason
- The refusal explains itself, saying *"0% is a halt on the first small loss"*
  or *"80% is not a cap at all"*, because a silent clamp teaches the operator
  the control does not work.

**Regression discipline.** Most of the 541 tests exist because something broke.
Each encodes symptom → root cause → the check that now catches it. Examples:

- A rate tuned on hourly bars was applied per tick. The live loop ran 60× faster
  than the backtest, and every test passed because the tests also walked bars.
- Git cannot store an empty directory, so the launcher died on a fresh clone at
  a path present on every developer machine.
- A string prefix is not a parent directory, and a correctly-placed audit log
  reported itself compromised.
- Sample data with backdated timestamps aged every "has this run lately" check,
  so a five-minute-old install accused itself.

### 4.4 The domain swap

The oracle problem is *the* shared problem between this and perception testing:
you cannot enumerate correct outputs, so you assert properties, use metamorphic
relations, and build scenario regression from real incidents. That is the same
answer for a trading model and a pedestrian detector.

**The sentence:** *"The hard part was the oracle problem. There is no ground
truth to assert against, so the tests assert properties instead: metamorphic
relations, structural invariants, and a statistical control that must score
exactly at chance. Everything else is regression built from real incidents, one
test per incident, each naming the root cause."*

---

## 5. Quality engineering and SQA

### 5.1 The named process

- **ISO/IEC 25010:2023**, *SQuaRE — Product quality model*. Revised in 2023:
  **nine** characteristics, not the eight people still quote, and *Safety* is
  now one of them. *Usability* became *Interaction capability*; *Portability*
  became *Flexibility*. Quality-in-use moved to ISO/IEC 25019:2023.
- **ISO 9001:2026**, *Quality management systems — Requirements*. ISO 9001:2015
  was **withdrawn on 16 September 2026**. Citing 2015 as current is a tell.
- **FRACAS** is the Failure Reporting, Analysis and Corrective Action System.
  Originates in MIL-STD-2155(AS) (1985), superseded by the handbook
  MIL-HDBK-2155 (1995). It is a *closed-loop* process: a failure is not closed
  until the corrective action is verified effective.
- **CAPA** is FDA device-regulation vocabulary (21 CFR 820.100), **not** ISO
  9001 wording. ISO 9001:2015 clause 10.2 is *"Nonconformity and corrective
  action"*; the separate preventive-action clause was deliberately removed in
  2015 and replaced by risk-based thinking. Do not attribute CAPA to ISO 9001.

### 5.2 The steps: closed-loop corrective action

1. **Detect and report** the failure.
2. **Contain** it, and stop the bleeding.
3. **Analyse root cause** with 5 Whys, fishbone, fault tree. (No governing
   standard for 5 Whys or 8D; 8D originated at Ford as TOPS in 1987. Bow-tie is
   described in IEC 31010:2019. Say "technique", not "standard".)
4. **Corrective action** fixes this occurrence.
5. **Preventive/systemic action** stops the class recurring.
6. **Verify effectiveness.**
7. **Close, and feed back into the process.**

### 5.3 What this project did

The project maintains a **failure log where every entry is one incident**:
symptom, root cause, the class of bug it belongs to, and the specific check that
now catches it. That is FRACAS structure, applied to a personal codebase.

A worked example, end to end:

1. **Detect.** A fresh install compiled dependencies for 15+ minutes and failed.
2. **Contain.** Stopped the install.
3. **Root cause.** A missing binary wheel is *not an error*: pip silently
   builds from source. The pinned versions had no build for the interpreter the
   installer selected.
4. **Corrective.** Repinned to versions with prepared builds for every
   supported interpreter.
5. **Preventive.** Made the first install attempt binary-only, so a future gap
   fails in seconds with a readable message instead of an hour of silence.
6. **Verify effectiveness.** Install time measured at **~30 seconds**.
7. **Systemic.** The first fix checked six packages *by name*; the next failure
   was a transitive dependency nobody lists. The check now resolves the **whole
   dependency graph** across four interpreter versions and two CPU
   architectures, or 8 combinations.

That last step is the interview-worthy part: **the first corrective action was
insufficient, the class recurred, and the fix was generalised.** Saying that out
loud demonstrates closed-loop thinking better than any success story.

### 5.4 Product quality characteristics, made concrete

| ISO/IEC 25010:2023 characteristic | evidence here |
|---|---|
| Functional suitability | 541 tests |
| Performance efficiency | Per-phase tick timing instrumented and displayed |
| Reliability | 9 liveness-monitored mechanisms; kill switch; auto-clearing day stop |
| **Safety** *(new in 2023)* | 17 pre-trade gates; capability for real orders removed entirely |
| Maintainability | 58 test files; incident-linked comments naming the failure each guard prevents |
| Interaction capability | The transparency UI |

**The sentence:** *"I ran a closed-loop corrective action process that was
FRACAS in shape if not in name. Every defect gets root cause, a corrective
action, and a preventive action at the class level, and it isn't closed until
the preventive action is verified. One of them I got wrong the first time: I
fixed the instance and the class recurred, so the second fix generalised the
check across the whole dependency graph."*

---

## 6. Data pipeline and system architecture

### 6.1 The steps

1. **Ingestion.** Source, protocol, cadence, failover.
2. **Validation at the boundary**, rejecting bad input before it becomes state.
3. **Storage and schema.** Normalisation, keys, indices, retention.
4. **Feature computation** of derived variables, under causality constraints.
5. **Serving.** What the consumer reads, and how fresh it must be.
6. **Observability** of freshness, completeness and drift.

### 6.2 What this project did, with numbers

| stage | detail |
|---|---|
| Ingestion | Public REST endpoints, **Coinbase primary with Kraken failover**, ~**50 instruments**, hourly bars plus live quotes, **15-second** loop |
| Boundary validation | A `feed_cross_check` gate compares sources; a `prices_on_screen_are_fresh` invariant enforces a staleness budget |
| Schema | **15 base tables, 132 columns**; `bars` keyed on (symbol, granularity, timestamp, source) so the same bar from two feeds cannot collide |
| Feature computation | Signal features written at decision time and frozen. Range lookbacks use `ts < ?`, which is **strictly before**, so a feature can never read the bar it is predicting |
| Reference data | A conditional survival table over **4.9M historical observations**, mapping (target ÷ hourly range, hours elapsed, current P&L) → probability the target is still reached |
| Serving | REST, ~**198 routes**; the UI polls with per-endpoint intervals tuned to how fast each figure changes |
| Efficiency | Shared TTL cache on hot reads because every open tab polls the same status route; per-phase tick timing instrumented, so a slow tick can be attributed to feed, strategy or database |

**Causality is the part to emphasise.** The `ts < ?` detail is a one-character
difference from `ts <= ?` and it is the difference between a valid feature and
temporal leakage. That is the same class of error as training on data recorded
after the event you are predicting.

### 6.3 The domain swap

| here | elsewhere |
|---|---|
| Two price feeds cross-checked | Redundant sensors cross-checked before fusion |
| Staleness budget on displayed prices | Maximum age on a displayed telemetry value |
| Features frozen at decision time | Perception inputs logged with the frame that produced the decision |
| `ts < ?` causality | No future information in a replay or a training window |

**The sentence:** *"Ingestion from two independent sources with automatic
failover, boundary validation before anything becomes state, a normalised schema
of fifteen tables keyed so two sources cannot collide, and features frozen at
decision time with a strict causality constraint so nothing can read forward.
About fifty instruments on a fifteen-second loop, and the reference table behind
the odds estimate is built from 4.9 million historical observations."*

---

## 7. Model and statistical validation

### 7.1 The steps

1. **Define the target** and the unit of observation.
2. **Split**, remembering that a time series splits by time.
3. **Fit competing candidates**, including a null model.
4. **Score on held-out data** with uncertainty, not a point estimate.
5. **Decide** against a pre-declared rule.
6. **Report the search effort.**
7. **State what the result does not support.**

### 7.2 What this project did

1. Target: did the position close profitable **after costs**.
2. Split: oldest 70% train, newest 30% held out, read once. Never shuffled, and
   a test asserts no shuffle call exists in the split path.
3. Five entrants: all variables; all variables heavily regularised; strongest
   five refitted; single best variable; **and a model with no variables at all**.
4. Held-out AUC with a bootstrap confidence interval, plus Brier and log-loss.
5. Decision rule declared in advance: a champion must have its **interval clear
   0.50**. The leader scored **AUC 0.786** with a 95% interval of
   **[0.40, 1.00]** on 11 held-out observations → **no champion declared**.
6. Trial count is part of the output, following the deflated-Sharpe argument
   that a result must be judged against how hard you searched for it.
7. Stated plainly: no user study was run, so no claim is made that the interface
   improves operator decisions.

**Why the null model matters.** Without a variable-free entrant in the table,
the best of five uninformative models reads as a winner. The control scores
0.500 by construction, so anything that cannot beat it has learned nothing. That
fact is only visible if it is on the page.

**The sentence:** *"Five candidate models per strategy, including a null model
as a control, split by time and scored on held-out data with bootstrap
confidence intervals. The decision rule was declared in advance: no champion
unless its interval clears chance. The leader scored 0.786 with an interval from
0.40 to 1.00, so the system reports no champion rather than ranking noise."*

---

## 8. Lifecycle and traceability

**ISO/IEC/IEEE 15288:2023**, *Systems and software engineering — System life
cycle processes*. Supersedes the 2015 edition.

The chain here is enforced in the schema rather than kept in a document:

```
need
 └─ hazard / unsafe control action
     └─ derived constraint  ──────────────►  one of 17 named gates
         └─ implementation  ──────────────►  guards.py
             └─ verification ─────────────►  test + invariant
                 └─ runtime evidence ─────►  gate reading shown in the UI
                     └─ incident ─────────►  failure-log entry + regression test
```

Every layer is reachable from the one above. That is what "bidirectional
traceability" means when it is real rather than a spreadsheet.

---

## 9. The domain swap, in one table

| this project | trading / model risk | AV / robotics | aerospace / UAS | manufacturing / process |
| --- | --- | --- | --- | --- |
| Instrument | Instrument, symbol | Vehicle, agent, track | Aircraft, airspace | Line, station, batch |
| Signal | Alpha signal, forecast | Detection, intent estimate | Sensed state | Measurement |
| Gate chain | Pre-trade risk controls (15c3-5) | Behaviour safety envelope | Run-time assurance monitor | Interlock chain |
| Day stop | Kill switch, daily loss limit | Operational limit / ODD boundary | Envelope limit | Andon stop |
| Cost hurdle | Transaction cost analysis; implementation shortfall | Minimum performance margin | Required safety margin | Process capability index |
| Correlated positions | Concentration / factor exposure | Common-cause sensor failure | Shared bus / common mode | Shared calibration source |
| Liveness check | Control testing. Has the limit ever bound? | Has the fallback ever engaged? | Has the monitor ever tripped? | Has the interlock ever fired? |
| Failure log | Incident and near-miss reporting | Incident database | Occurrence reporting | Nonconformance log |
| Model arena | Challenger model, benchmarking (SR 26-2) | Perception model selection | Algorithm qualification | Gauge R&R / method comparison |

### 9.1 The case where no swap is needed

A trading firm, an exchange or a bank is the one audience for whom the second
column is not a translation. There the vocabulary is already fixed, and it is
worth using exactly:

- **Model risk** is defined by the Federal Reserve's **SR 26-2** (17 April
  2026), which replaced SR 11-7 (2011) and SR 21-8 and was issued jointly with
  **OCC Bulletin 2026-13** and an FDIC FIL. The definition: "the potential for
  adverse financial consequences associated with models, which may result from
  decisions made based on model output." A *model* is "a complex quantitative
  method, system, or approach that applies statistical, economic, or financial
  theories to process input data into quantitative estimates."
- **Effective challenge** is the load-bearing term, defined as "the critical
  analysis conducted by objective experts who evaluate model risk and effect
  appropriate changes throughout the model lifecycle". It is what §7 of this
  document describes: a no-variable control model in every field, a champion
  refused when its bootstrap interval fails to clear chance, and a strategy
  retired on its own evidence.
- **The three validation elements** survive the revision unchanged:
  *conceptual soundness*, *ongoing monitoring*, *outcomes analysis*. They map
  one-to-one onto §7's split discipline, §3's liveness checks and §6's
  ledger-versus-outcome reconciliation.
- **Pre-trade risk controls** under **SEC Rule 15c3-5**, the market access rule,
  require controls that reject erroneous orders and enforce credit and capital
  thresholds *before* the order reaches the venue. It is still a FINRA
  examination priority in its 2026 oversight report. That is the gate chain, and
  it is the same shape: seventeen named refusals, each of which must say which
  one fired.

Two cautions. SR 26-2 is expected to be "most relevant to banking
organizations with over $30 billion in total assets," so it is a vocabulary and
a frame rather than a rule that binds a personal project. Say so before someone
else does. And it explicitly places generative and agentic AI outside its scope,
which is a distinction worth knowing when the conversation turns to LLM
governance.

The honest positioning for this audience is **validation, not alpha**. The
project's result is that the strategy does not clear its own cost floor. Anyone
hiring for a trading seat will read a retail bot as a hobby; anyone hiring for
model validation, model risk, trade surveillance, market-risk control or
production risk will read the same repository as someone who built a measurement
apparatus and then let it kill the thing it was built to support. The second
reading is the true one, and it is the one to lead with.

---

**The method does not change. Only the nouns do.** That sentence is worth
memorising, because it is the honest answer to *"but this is trading, we do
vehicles."*

---

## 10. Answering the question out loud

A repeatable four-beat structure. It stops the two common failures, which are
trailing off after one sentence and narrating chronologically for five minutes.

1. **Name the process.** *"That's hazard analysis. I used an STPA-style frame."*
2. **Give the steps.** *"Control structure, unsafe control actions, loss
   scenarios, derived constraints, verification."*
3. **Give one concrete example with a number.** *"One unsafe control action was
   an order placed while the day's loss budget was exhausted. That became an
   enforced gate, one of seventeen, and each records its limit and its
   reading."*
4. **Say what it cost or caught.** *"A separate liveness check asks whether each
   safeguard has ever actually fired, because a trailing stop was reported as
   working for weeks while its code block was never entered."*

Beat 4 is the one most people skip, and it is the one that makes the rest
credible. **The measured failure is the evidence. The success story is the
claim.**

Three more, ready to use:

- *"What's your testing approach?"* → **§4.3**, lead with the oracle problem.
- *"Have you done risk assessment?"* → **§2.3**, the eight ISO 31000 steps with
  the 1.90%-vs-2–3% number at step 3.
- *"How do you validate a model?"* → **§7.2**, lead with the null control.

---

## 11. What not to claim

Keep this project and employment history separate, and keep both accurate.

**About this project, these are true:**
- Built a hazard-driven gate chain and a closed-loop failure log.
- Designed and ran held-out model validation with a null control.
- Implemented traceability from decision record to execution record.
- Produced and reported a **negative** validation result.

**Do not say, about this project:**
- That it is ISO 26262, ISO 9001 or UL 4600 **compliant**. It is not, and it was
  not assessed. Say *"I used the structure of"* or *"a HARA-style frame"*.
- That you authored a formal FMEA, FTA or FHA. Knowing the methods and having
  authored one are different claims.
- That a certification body, auditor or customer reviewed any of it.
- That the transparency UI improved operator performance. **No user study was
  run.** Volunteering this limit is more credible than the claim would be.

**On standards, cite the current edition:** ISO 9001:**2026** (2015 withdrawn
16 Sep 2026) · IEEE 1012-**2024** (not 2016, and it is *System, Software, and
Hardware*) · ISO/IEC 25010:**2023** (nine characteristics, *Product quality
model*) · ISO/IEC/IEEE 15288:**2023** · ISO 31000:**2018** (*Guidelines*, not
*Principles and guidelines*). Cite IEC 61508 **by part**. SAE J3016 is a
*Recommended Practice*, and it deprecates the words "autonomous" and
"self-driving". The STPA Handbook is a handbook, not a standard.

---

## 12. Standards and sources

| designation | title | edition |
|---|---|---|
| ISO 31000 | Risk management — Guidelines | 2018 |
| IEC 31010 | Risk management — Risk assessment techniques | 2019 |
| IEEE Std 1012 | System, Software, and Hardware Verification and Validation | 2024 |
| ISO/IEC/IEEE 29119 | Software and systems engineering — Software testing | -1:2022, -2:2021, -3:2021, -4:2021, -5:2024 |
| ISO/IEC TR 29119-11 | Testing of AI-based systems | 2020 |
| ISO/IEC 25010 | SQuaRE — Product quality model | 2023 |
| ISO/IEC 25019 | SQuaRE — Quality-in-use model | 2023 |
| ISO 9001 | Quality management systems — Requirements | 2026 |
| ISO/IEC/IEEE 15288 | System life cycle processes | 2023 |
| ISO 26262 | Road vehicles — Functional safety | 2018 |
| ISO 21448 | Road vehicles — Safety of the intended functionality (SOTIF) | 2022 |
| IEC 61508 | Functional safety of E/E/PE safety-related systems (cite by part) | Ed. 2.0, 2010 |
| UL 4600 | Evaluation of Autonomous Products | 3rd ed., 2023 |
| SAE J3016 | Taxonomy and Definitions for Terms Related to Driving Automation Systems | J3016_202104 |
| Leveson & Thomas | *STPA Handbook* | 2018, MIT PSAS |
| Leveson | *Engineering a Safer World* | MIT Press, 2011/2016 — open access |
| MIL-HDBK-2155 | Failure Reporting, Analysis and Corrective Action Taken | 1995 (handbook, not a requirements standard) |
| SR 26-2 (Fed) / OCC 2026-13 | Revised Guidance on Model Risk Management — replaces SR 11-7 (2011) and SR 21-8 (2021) | 17 Apr 2026 |
| SEC Rule 15c3-5 | Risk Management Controls for Brokers or Dealers with Market Access | adopted 2010, in force |

**No governing standard:** 5 Whys (Toyota practice, Ohno 1988) · 8D (Ford TOPS,
1987) · bow-tie as a method, though it is described in IEC 31010:2019.
