import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'
import StrategyDays from '../components/StrategyDays.jsx'
import { DayBars } from '../components/charts.jsx'

/* One report per day, built automatically and kept. Arrows walk the history;
 * the range view aggregates and charts it.
 *
 * The number that matters here is COVERAGE: of the moves that were genuinely
 * worth taking, how many did any strategy even look at. A gap there is a
 * missing strategy, which is a different problem from a risk block. */

const iso = (d) => d.toISOString().slice(0, 10)
const shift = (day, n) => {
  const d = new Date(day + 'T12:00:00Z')
  d.setUTCDate(d.getUTCDate() + n)
  return iso(d)
}

/* The per-day charts live in components/charts.jsx (DayBars). This page used to
   carry its own inline SVG whose only explanation was a native <title>: it needed
   a mouse held still for a second, and on a phone it did not exist at all, so on
   the device this app is mostly read on the bars were decoration. */

export default function Daily() {
  const [day, setDay] = useState(iso(new Date()))
  const [rangeDays, setRangeDays] = useState(14)
  const rep = useAsync(() => api.reportDay(day), [day], 120000)
  const end = iso(new Date())
  const start = shift(end, -rangeDays)
  const rng = useAsync(() => api.reportRange(start, end), [rangeDays], 300000)

  const rt = useAsync(() => api.routing('paper'), [], 600000)
  const sc = useAsync(() => api.scorecard(), [], 300000)
  const ev = useAsync(() => api.evolve(), [], 600000)
  const rtn = useAsync(() => api.retrain(), [], 600000)
  const inv = useAsync(() => api.invariants(), [], 300000)
  const sz = useAsync(() => api.sizing('paper'), [], 300000)
  const [exitScope, setExitScope] = useState('day')
  const pend = useAsync(() => api.pending(), [], 600000)
  const shape = useAsync(() => api.dayShape(), [], 600000)
  const xl = useAsync(
    () => (exitScope === 'day' ? api.exitLab(day, day) : api.exitLab(start, end)),
    [day, exitScope, rangeDays], 300000)
  const d = rep.data
  const c = d?.counts || {}
  const p = d?.pnl || {}
  const rows = rng.data?.rows || []
  const tot = rng.data?.totals || {}

  return (
    <>
      <div className="row" style={{ marginBottom: 4 }}>
        <h1 style={{ margin: 0 }}>Daily</h1>
        <div className="spacer" />
        <button onClick={() => setDay(shift(day, -1))}>← prev</button>
        <input type="date" value={day} max={iso(new Date())}
               onChange={(e) => e.target.value && setDay(e.target.value)} />
        <button disabled={day >= iso(new Date())} onClick={() => setDay(shift(day, 1))}>next →</button>
        <button onClick={() => setDay(iso(new Date()))}>today</button>
      </div>
      <p className="sub">
        Built automatically every six hours and stored, so the history is stable.
        <Info text="An opportunity is graded against that coin's own round trip: strong is a best move over 3x the spread, workable 2-3x, marginal 1-2x. Marginal ones need a near-perfect exit to net anything, so coverage is measured against strong + workable only." />
      </p>

      {rep.err && <Banner kind="bad" title="could not load this day">{String(rep.err)}</Banner>}

      <div className="grid c4" style={{ marginBottom: 8 }}>
        <Stat small label="worth taking" value={c.workable ?? '—'}
              meta={`${c.worth_taking ?? 0} incl. marginal`} />
        <Stat small label="nothing was watching" value={c.uncovered_workable ?? '—'}
              meta="a missing strategy" />
        <Stat small label="seen, not taken" value={c.missed ?? '—'} meta="a risk block" />
        {/* ONE number, and it is the same one the Journal sums. Everything on
            every tab that calls itself P&L is NET OF SPREAD; the spread is shown
            beside it, never instead of it, because it is a venue cost we are
            tracking on purpose (see below) and not a result. */}
        <Stat small label="net P&L (after spread)"
              value={p.net_usd != null ? fmt.usd(p.net_usd) : '—'}
              meta={`${c.trades_closed ?? 0} trade${c.trades_closed === 1 ? '' : 's'} closed`} />
      </div>

      {p.net_usd != null && (
        <div className="mut" style={{ fontSize: 12, margin: '0 0 8px' }}>
          {c.trades_closed ?? 0} closed trade{c.trades_closed === 1 ? '' : 's'} moved
          {' '}<b className={(p.gross_usd ?? 0) >= 0 ? 'pos' : 'neg'}>{fmt.usd(p.gross_usd)}</b>
          {' '}on the coins and paid <b>{fmt.usd(p.cost_usd)}</b> to Robinhood in spread,
          leaving <b className={(p.net_usd ?? 0) >= 0 ? 'pos' : 'neg'}>{fmt.usd(p.net_usd)}</b>.
          <Info text={'Three different numbers, and only the last one is "how the day went".\n\n'
            + 'MOVED is what the coins did — before it cost anything to be in them.\n'
            + 'SPREAD is what Robinhood charged to get in and out. It is tracked separately '
            + 'on purpose, so that when we price up another venue we can see exactly what '
            + 'this one costs us.\n'
            + 'NET is moved minus spread, and it is the only one to read as profit. It is the '
            + 'same figure the Journal shows per trade, so the two tabs always agree — if '
            + 'they ever do not, that is a bug, please say so.\n\n'
            + 'A single losing TRADE is a different thing again: one trade can be -$7 on a '
            + 'day that nets +$42.'} />
        </div>
      )}

      {!!(d?.lessons || []).length && (
        <Card collapse="daily.what-the-day-taught" title="what the day taught">
          <ul style={{ margin: '2px 0', paddingLeft: 18 }}>
            {d.lessons.map((l, i) => <li key={i} style={{ marginBottom: 3 }}>{l}</li>)}
          </ul>
        </Card>
      )}

      <Card collapse="daily.nothing-was-watching" title="nothing was watching — this list is the spec for the next strategy">
        <Table
          cols={[
            { key: 'symbol', label: 'coin' },
            { key: 'best_from_open_pct', label: 'best move', num: true,
              render: (r) => <span className="pos">+{r.best_from_open_pct.toFixed(2)}%</span> },
            { key: 'margin_x', label: 'x cost', num: true,
              render: (r) => `${r.margin_x}x` },
            { key: 'grade', label: 'grade',
              render: (r) => <span className={`pill ${r.grade === 'strong' ? 'ok' : 'warn'}`}>{r.grade}</span> },
            { key: 'change_pct', label: 'close', num: true,
              render: (r) => <span className={r.change_pct >= 0 ? 'pos' : 'neg'}>{r.change_pct.toFixed(2)}%</span> },
            { key: 'drawdown_pct', label: 'worst', num: true,
              render: (r) => <span className="neg">{r.drawdown_pct.toFixed(2)}%</span> },
            { key: 'round_trip_pct', label: 'its cost', num: true,
              render: (r) => `${r.round_trip_pct.toFixed(2)}%` },
          ]}
          rows={d?.uncovered || []}
          empty="every workable move was seen by at least one strategy"
        />
        {!!(d?.uncatchable || []).length && (
          <div className="step-detail" style={{ marginTop: 6 }}>
            plus {d.uncatchable.length} of the slow-drift shape — measured and deliberately
            not chased ({d.uncatchable[0]?.why_not}):{' '}
            {d.uncatchable.map((x) => x.symbol).join(', ')}
          </div>
        )}
        {!!(d?.uncovered_marginal || []).length && (
          <div className="step-detail" style={{ marginTop: 6 }}>
            plus {d.uncovered_marginal.length} marginal (under 2x cost), not worth a rule:{' '}
            {d.uncovered_marginal.map((x) => x.symbol).join(', ')}
          </div>
        )}
      </Card>

      {/* ── STRATEGY LEVEL: the league over the range, then one block per strategy
          with its days, what changed each day, its belief and its exits. ── */}
      <h2>Strategies — day by day<Info text="Each strategy over the selected range: the league table (dollars, per-trade percent, win rate with a 90% interval, days positive, drawdown, profit factor, spread paid, signals taken, belief), a day-by-day grid for head-to-head, and per strategy the day rows with rank among peers, the Welch verdict against its own earlier trades, and the changes the desk recorded that day. Hide any block with the ▾ — it stays hidden." /></h2>
      <StrategyDays start={start} end={end} selectedDay={day} />


      <div className="grid c2">
        <Card collapse="daily.what-each-strategy-did" title="what each strategy did">
          <Table
            cols={[
              { key: 'strategy', label: 'strategy' },
              { key: 'signals', label: 'signals', num: true },
              { key: 'taken', label: 'taken', num: true },
              { key: 'coins', label: 'coins', num: true },
            ]}
            rows={d?.strategies || []}
            empty="no strategy produced a signal on this day"
          />
          {(d?.strategies || []).map((s) => s.top_reject_reasons?.length ? (
            <div key={s.strategy} className="step-detail" style={{ marginTop: 5 }}>
              <b>{s.strategy}</b> — {s.top_reject_reasons[0].n}x: {s.top_reject_reasons[0].reason}
            </div>
          ) : null)}
        </Card>

        <Card collapse="daily.what-we-traded" title="what we traded">
          <Table
            cols={[
              { key: 'symbol', label: 'coin' },
              { key: 'strategy', label: 'strategy' },
              { key: 'net_pnl_usd', label: 'net', num: true,
                render: (r) => <span className={r.net_pnl_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_pnl_usd)}</span> },
              { key: 'gross_pnl_usd', label: 'coin moved', num: true,
                render: (r) => fmt.usd(r.gross_pnl_usd) },
              { key: 'cost_usd', label: 'spread', num: true, render: (r) => fmt.usd(r.cost_usd) },
              { key: 'holding_s', label: 'held', num: true,
                render: (r) => `${(r.holding_s / 3600).toFixed(1)}h` },
            ]}
            rows={d?.closed || []}
            empty="nothing closed on this day"
          />
        </Card>
      </div>

      {/* What the round trip actually costs, and why it is not a constant.
          From Robinhood's own API reference: v1 prices come from market makers
          WITH a spread built in; v2 prices come from partner exchanges and the
          cost is a separate volume-tiered fee. Every result in this app assumed
          the first and never modelled the second. */}
      {rt.data && (
        <Card collapse="daily.what-a-round-trip-costs-and-what-it-woul" title="what a round trip costs, and what it would cost">
          <div className="row" style={{ marginBottom: 6 }}>
            <span className="pill warn">routing in use: market maker (default)</span>
            <span className="pill">30d volume {fmt.usd(rt.data.trailing_30d_volume_usd)}</span>
            <span className="pill">
              tier {rt.data.tier?.taker_pct?.toFixed(3)}% taker
            </span>
            {rt.data.tier?.volume_to_next_usd > 0 && (
              <span className="pill">
                {fmt.usd(rt.data.tier.volume_to_next_usd)} more → {rt.data.tier.next_tier_taker_pct?.toFixed(3)}%
              </span>
            )}
            <Info text={rt.data.routing_note + ' ' + rt.data.maker_note} />
          </div>
          <Table
            cols={[
              { key: 'strategy', label: 'strategy' },
              { key: 'gross_pct', label: 'held-out gross', num: true,
                render: (r) => `${r.gross_pct >= 0 ? '+' : ''}${r.gross_pct.toFixed(2)}%` },
              ...((rt.data.cost_levels || []).map((lv, i) => ({
                key: `lv${i}`, label: `${lv.label} (${lv.round_trip_pct.toFixed(2)}%)`, num: true,
                render: (r) => {
                  const v = r.net?.[i]?.net_pct
                  if (v == null) return '—'
                  return <span className={v >= 0 ? 'pos' : 'neg'}>{v >= 0 ? '+' : ''}{v.toFixed(2)}%</span>
                },
              }))),
            ]}
            rows={rt.data.strategy_net_by_level || []}
            empty="no measured strategies"
          />
        </Card>
      )}

      {/* Every closed trade is evidence for or against the backtest. Nothing
          compared the two until now: morning_dip was measured at -1.71% a trade
          and then lost $18.64 over eight live trades with nothing noticing. */}
      {sc.data?.cards?.length > 0 && (
        <Card collapse="daily.what-the-real-trades-say-against-what-th" title="what the real trades say, against what the backtest promised">
          {!!sc.data.paused?.length && (
            <Banner kind="warn" title="paused by the evidence">
              {sc.data.paused.join(', ')} — the daily pass will resume them if the live
              interval climbs back above zero.
            </Banner>
          )}
          <Table
            cols={[
              { key: 'strategy', label: 'strategy' },
              { key: 'n', label: 'scored', num: true,
                info: 'Trades used to judge the rule. Trades our own bugs produced are excluded here and kept everywhere else — the money moved, but a rule cannot be judged on a trade it did not choose.' },
              { key: 'n_excluded_defective', label: 'excluded', num: true,
                render: (r) => (r.n_excluded_defective
                  ? <span className="warn" title={Object.keys(r.defects || {}).join(', ')}>
                      {r.n_excluded_defective}
                    </span>
                  : <span className="mut">0</span>) },
              { key: 'mean_net_pct', label: 'live net/trade', num: true,
                render: (r) => r.mean_net_pct == null ? '—'
                  : <span className={r.mean_net_pct >= 0 ? 'pos' : 'neg'}>{r.mean_net_pct.toFixed(2)}%</span> },
              { key: 'expected_net_pct', label: 'backtest said', num: true,
                render: (r) => r.expected_net_pct == null ? '—' : `${r.expected_net_pct.toFixed(2)}%` },
              { key: 'ci90', label: '90% interval',
                render: (r) => r.ci90?.[0] == null ? '—' : `${r.ci90[0].toFixed(2)}% … ${r.ci90[1].toFixed(2)}%` },
              { key: 'spread_took_pct', label: 'spread took', num: true,
                render: (r) => r.spread_took_pct == null ? '—' : `${r.spread_took_pct.toFixed(2)}%` },
              { key: 'win_rate_pct', label: 'wins', num: true,
                render: (r) => r.win_rate_pct == null ? '—' : `${r.win_rate_pct}%` },
              { key: 'median_hold_h', label: 'held', num: true,
                render: (r) => r.median_hold_h == null ? '—' : `${r.median_hold_h}h` },
            ]}
            rows={sc.data.cards}
            empty="no closed trades yet"
          />
          {(sc.data.cards || []).map((cd) => (
            <div key={cd.strategy} className="step-detail" style={{ marginTop: 4 }}>
              <b>{cd.strategy}</b> — {cd.verdict}
            </div>
          ))}
        </Card>
      )}

      {/* Ideas waiting on evidence. Every one names the exact quantity it is
          short of, so a deferred idea is a countdown rather than a thing that
          quietly never happened. */}
      {pend.data && (
        <Card collapse="daily.waiting-on-data" title={`waiting on data — ${pend.data.n_ready || 'no'} idea(s) ready to act on`}>
          <Table
            cols={[
              { key: 'idea', label: 'idea' },
              { key: 'progress', label: 'progress', num: true,
                render: (r) => `${r.have} / ${r.need}` },
              { key: 'unit', label: 'waiting for' },
              { key: 'ready', label: '',
                render: (r) => (r.fired_at ? <span className="pill ok">triggered</span>
                  : r.ready ? <span className="pill warn">ready</span>
                  : <span className="mut">{r.short_by} to go</span>) },
              { key: 'why_not_yet', label: 'why not yet' },
            ]}
            rows={pend.data.experiments || []}
            empty="nothing deferred"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>{pend.data.rule}</div>
        </Card>
      )}

      {/* Exit lab. Every closed trade replayed against every exit rule we
          know of — same entries, same costs, no money at risk. It never changes
          how anything trades; it accumulates the evidence that a change would
          have to clear. */}
      {/* THE BEAR-DAY QUESTION, answered on every coin-day the venue has rather
          than on the one ZEC day that raised it. */}
      <Card collapse="daily.when-the-day-has-turned-bear" title="when the day has turned bear — does the rest of the day keep falling?">
        <p className="mut" style={{ fontSize: 12, marginTop: 0 }}>
          Every coin-day in the hourly history, read at 06:00, 09:00 and 12:00 Austin. Bear = below the day's open and making lower highs and lower lows over the last W hours than the W before. No percentage anywhere in the definition.
          <Info text="Rest-of-day is the day's last close against the price at the reading hour, before costs. 'Further drawdown' is how much lower it went before the close, on average. The verdict compares bear days to the rest AND sizes the expected move against one side of Robinhood's spread — a move smaller than the spread is not a reason to sell. The exit lab's bear_cut_* rules are the same shape applied to the desk's own trades." />
        </p>
        {shape.err ? <div className="neg">{shape.err}</div>
          : !shape.data?.available ? <div className="mut">{shape.data?.why || 'waiting for the first daily run'}</div>
          : (
            <>
              <Table
                cols={[
                  { key: 'k', label: 'read at · window' },
                  { key: 'bn', label: 'bear days', num: true },
                  { key: 'br', label: 'rest of day', num: true,
                    render: (r) => <span className={r.br < 0 ? 'neg' : 'pos'}>{r.br >= 0 ? '+' : ''}{r.br.toFixed(2)}%</span> },
                  { key: 'bp', label: 'ended lower', num: true, render: (r) => `${r.bp.toFixed(0)}%` },
                  { key: 'bd', label: 'further drawdown', num: true, render: (r) => `${r.bd.toFixed(2)}%` },
                  { key: 'nr', label: 'not-bear rest', num: true,
                    render: (r) => <span className={r.nr < 0 ? 'neg' : 'pos'}>{r.nr >= 0 ? '+' : ''}{r.nr.toFixed(2)}%</span> },
                  { key: 'sg', label: 'σ', num: true, render: (r) => r.sg.toFixed(1) },
                  { key: 'v', label: 'verdict' },
                ]}
                rows={Object.entries(shape.data.cells).sort(([a], [b]) => a.localeCompare(b))
                  .filter(([, c]) => c.bear?.n && c.not_bear?.n)
                  .map(([k, c]) => ({
                    k: `${k.slice(1, 3)}:00 · ${k.slice(-1)}h`, bn: c.bear.n.toLocaleString(),
                    br: c.bear.mean_rest_pct, bp: c.bear.p_negative, bd: c.bear.mean_further_drawdown_pct ?? 0,
                    nr: c.not_bear.mean_rest_pct, sg: c.sigmas ?? 0, v: c.verdict,
                  }))} />
              <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
                {shape.data.coins} coins × {shape.data.days.toLocaleString()} days · {shape.data.definition}
                {shape.data.ran_at && ` · ran ${fmt.time(shape.data.ran_at)}`}
              </div>
            </>
          )}
      </Card>

      {xl.data && (
        <Card collapse="daily.exit-lab" title="exit lab — what other exits would have returned">
          <div className="row" style={{ gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
            <div className="seg">
              <button className={exitScope === 'day' ? 'on' : ''}
                      onClick={() => setExitScope('day')}>this day</button>
              <button className={exitScope === 'range' ? 'on' : ''}
                      onClick={() => setExitScope('range')}>last {rangeDays} days</button>
            </div>
            <div className="spacer" />
            <span className="mut" style={{ fontSize: 12 }}>
              {xl.data.n_trades} trade(s) replayed
              {xl.data.actual_mean_pct != null &&
                ` · what actually happened: ${xl.data.actual_mean_pct >= 0 ? '+' : ''}${xl.data.actual_mean_pct.toFixed(2)}%/trade`}
            </span>
          </div>
          {xl.data.giveback && (
            <div className="grid c3" style={{ marginBottom: 10 }}>
              <Stat small label="best it ever showed"
                    value={`${xl.data.giveback.best_shown_pct >= 0 ? '+' : ''}${xl.data.giveback.best_shown_pct.toFixed(2)}%`}
                    meta="average peak, net of both spreads" />
              <Stat small label="what we actually got"
                    value={`${xl.data.giveback.actually_got_pct >= 0 ? '+' : ''}${xl.data.giveback.actually_got_pct.toFixed(2)}%`}
                    tone={xl.data.giveback.actually_got_pct >= 0 ? 'ok' : 'bad'}
                    meta="average exit" />
              <Stat small label="given back"
                    value={`${xl.data.giveback.given_back_pts.toFixed(2)} pts`}
                    tone="bad"
                    meta="what a better exit is competing for" />
            </div>
          )}
          <Table
            cols={[
              { key: 'rule', label: 'exit rule',
                render: (r) => (
                  <span title={r.what}>
                    {r.is_live_rule && <span className="pill ok" style={{ marginRight: 6 }}>live</span>}
                    {r.what}
                  </span>) },
              { key: 'mean_net_pct', label: 'net / trade', num: true,
                render: (r) => (
                  <span className={r.mean_net_pct >= 0 ? 'pos' : 'neg'}>
                    {r.mean_net_pct >= 0 ? '+' : ''}{r.mean_net_pct.toFixed(2)}%
                  </span>),
                info: 'After both spreads, same entries the desk actually took.' },
              { key: 'se_pct', label: '± se', num: true,
                render: (r) => r.se_pct.toFixed(2) },
              { key: 'win_rate', label: 'win %', num: true,
                render: (r) => `${r.win_rate.toFixed(0)}%` },
              { key: 'median_hours', label: 'median hold', num: true,
                render: (r) => (r.median_hours == null ? '—' : `${r.median_hours.toFixed(1)}h`) },
              { key: 'n', label: 'n', num: true,
                info: 'How many closed trades this rule has been replayed on. A rule added '
                    + 'later is back-filled on every earlier trade, so all rows converge.' },
              { key: 'vs_live_pct', label: 'vs live', num: true,
                render: (r) => (r.is_live_rule ? '—' :
                  `${r.vs_live_pct >= 0 ? '+' : ''}${(r.vs_live_pct ?? 0).toFixed(2)}%`) },
              { key: 'verdict', label: 'verdict',
                render: (r) => (r.is_live_rule ? <span className="mut">as traded — the baseline</span> :
                  r.verdict === 'BETTER' ? <span className="pill ok">better</span> :
                  r.verdict === 'worse' ? <span className="pill bad">worse</span> :
                  <span className="mut">{r.verdict}</span>) },
            ]}
            rows={xl.data.rules || []}
            empty="no closed trades in this window yet"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            {xl.data.note} A rule needs {xl.data.min_for_a_verdict} replayed trades
            before any verdict is given, and two standard errors before it can
            replace the one running.
          </div>
        </Card>
      )}

      {/* THE GATE THAT DID NOT EXIST. Every check this repo had read the CODE;
          the NULL order linkage lived in the DATA and survived four days of
          green checks. These are post-conditions on the database. */}
      {inv.data && (
        <Card collapse="daily.data-invariants" title={`data invariants — ${inv.data.headline}`}>
          <Table
            cols={[
              { key: 'question', label: 'must be true' },
              { key: 'detail', label: 'what the data says' },
              { key: 'status', label: '',
                render: (r) => (r.status === 'ok'
                  ? <span className="pill ok">holds</span>
                  : r.status === 'warning'
                    ? <span className="pill warn">check</span>
                    : <span className="pill bad">broken</span>) },
            ]}
            rows={inv.data.results || []}
            empty="no invariants registered"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            Each one names a bug it would have caught. Hover a row in the table above
            for the question; the full worked example is in the API response and in
            backend/tests/test_data_invariants.py.
          </div>
        </Card>
      )}

      {/* Champion vs challenger. Refusal is the normal outcome and is reported
          as loudly as a promotion, because at this sample size most
          "improvements" are noise. */}
      {rtn.data && (
        <Card collapse="daily.retraining" title="retraining — challengers, and what happened to them">
          <Table
            cols={[
              { key: 'strategy', label: 'strategy' },
              { key: 'trigger', label: 'why it is due' },
              { key: 'days', label: 'days of data', num: true },
              { key: 'trades', label: 'clean trades', num: true },
            ]}
            rows={rtn.data.due || []}
            empty="nothing is due — no strategy has seen enough new data since its last fit"
          />
          {(rtn.data.waiting || []).length > 0 && (
            <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
              waiting: {rtn.data.waiting.map((w) => (
                `${w.strategy} (needs ${w.needs_days}d or ${w.needs_trades} trades)`
              )).join(' · ')}
            </div>
          )}
          <Table
            cols={[
              { key: 'strategy', label: 'past fits' },
              { key: 'verdict', label: 'verdict' },
              { key: 'is_champion', label: 'running?',
                render: (r) => (r.is_champion
                  ? <span className="pill ok">champion</span>
                  : <span className="mut">no</span>) },
            ]}
            rows={rtn.data.recent || []}
            empty="no fit has run yet"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            {rtn.data.rule}
          </div>
        </Card>
      )}

      {/* How much money the next trade gets, and what is left after it. There is
          no slot count anywhere in this: the number of positions is whatever the
          cash and the venue's minimum order size allow. */}
      {sz.data && (
        <Card collapse="daily.what-the-next-trade-would-get" title="what the next trade would get">
          <Table
            cols={[
              { key: 'strategy', label: 'strategy' },
              { key: 'signals_per_day', label: 'signals/day', num: true,
                render: (r) => (r.signals_per_day ?? 0).toFixed(1) },
              { key: 'share', label: 'share of cash', num: true,
                render: (r) => `${((r.share ?? 0) * 100).toFixed(0)}%` },
              { key: 'size_usd', label: 'next position', num: true,
                render: (r) => fmt.usd(r.size_usd) },
              { key: 'left_after', label: 'left after it', num: true,
                render: (r) => fmt.usd(r.left_after) },
              { key: 'why', label: 'how that number was reached' },
            ]}
            rows={sz.data.rows || []}
            empty="no strategies active"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            {sz.data.rule}
            {sz.data.floor_source === 'placeholder_until_universe_sync' &&
              ' — that minimum is a placeholder until the universe syncs Robinhood\u2019s own per-coin figure.'}
          </div>
        </Card>
      )}

      {/* What is accumulating toward a decision, and how far away it is. Nothing
          here ships a strategy: a rule built the moment a shape appears is how
          overfitting starts, and this project has three sessions of evidence. */}
      {ev.data && (
        <Card collapse="daily.accumulating" title="accumulating — what is not yet enough to act on">
          <Table
            cols={[
              { key: 'shape', label: 'shape nothing watched' },
              { key: 'coin_days', label: 'coin-days', num: true },
              { key: 'days_seen', label: 'days seen', num: true },
              { key: 'avg_best', label: 'avg best move', num: true,
                render: (r) => `${(r.avg_best ?? 0).toFixed(2)}%` },
              { key: 'ready_to_measure', label: 'status',
                render: (r) => (r.verdict
                  ? <span className="pill ok">{r.verdict}</span>
                  : r.ready_to_measure
                    ? <span className="pill warn">ready to measure</span>
                    : <span className="mut">{r.needs}</span>) },
            ]}
            rows={ev.data.shapes || []}
            empty="nothing accumulating yet — reports classify shapes as they build"
          />
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            A shape is measured only after {ev.data.thresholds?.min_days_seen} separate days
            and {ev.data.thresholds?.min_coin_days} coin-days, and measuring records a
            verdict — it never ships a rule.
          </div>
          <Table
            cols={[
              { key: 'strategy', label: 'live training rows' },
              { key: 'rows', label: 'usable', num: true },
              { key: 'rows_needed_to_fit', label: 'needed', num: true },
              { key: 'shortfall', label: 'short by', num: true },
              { key: 'orphans_no_features', label: 'unlinkable', num: true,
                render: (r) => (r.orphans_no_features
                  ? <span className="mut" title="opened before trades were linked to their signal">{r.orphans_no_features}</span>
                  : '0') },
              { key: 'ready_to_fit', label: 'fit yet?',
                render: (r) => (r.ready_to_fit
                  ? <span className="pill ok">yes</span>
                  : <span className="mut">no</span>) },
            ]}
            rows={ev.data.training || []}
            empty="no trades linked to signals yet"
          />
        </Card>
      )}

      <Card collapse="daily.last" title={`last ${rangeDays} days`}>
        <div className="row" style={{ marginBottom: 6 }}>
          {[7, 14, 30, 90].map((n) => (
            <button key={n} className={rangeDays === n ? 'primary' : ''}
                    onClick={() => setRangeDays(n)}>{n}d</button>
          ))}
          <div className="spacer" />
          <span className="pill">{tot.days ?? 0} days</span>
          <span className="pill">{tot.workable ?? 0} workable</span>
          <span className={`pill ${tot.coverage_pct >= 80 ? 'ok' : 'bad'}`}>
            {tot.coverage_pct != null ? `${tot.coverage_pct}% covered` : 'coverage —'}
          </span>
          <span className="pill">net {fmt.usd(tot.net_usd)}</span>
        </div>
        <div className="mut" style={{ fontSize: 11, marginBottom: 2 }}>workable moves nobody watched, per day</div>
        <DayBars
          rows={rows}
          views={[{ key: 'unwatched', label: 'unwatched', kind: 'bar',
                    pick: (r) => r.uncovered_workable ?? 0,
                    format: (n) => String(Math.round(n)),
                    hint: 'point at a bar to see that day' }]}
          detail={(r) => [
            ['unwatched', String(r.uncovered_workable ?? 0)],
            ['workable', String(r.workable ?? 0)],
            ['seen, not taken', String(r.missed ?? 0)],
          ]} />
        <div className="mut" style={{ fontSize: 11, margin: '10px 0 2px' }}>
          money, per day — switch between what each day made, what it has made in
          total, and what the spread cost to make it
        </div>
        <DayBars
          rows={rows}
          views={[
            { key: 'net', label: 'net per day', kind: 'bar',
              pick: (r) => r.net_usd ?? 0 },
            /* Cumulative gets its own view rather than a second line on the
               same axes: over 90 days the running total dwarfs any single day,
               so sharing an axis flattens the bars into nothing and giving it
               its own axis invents a relationship between two scales. */
            { key: 'cum', label: 'running total', kind: 'line',
              pick: (r, i, all) => all.slice(0, i + 1)
                                      .reduce((t, x) => t + (x.net_usd || 0), 0) },
            /* "spread paid", not "cost", and drawn in a neutral colour. Called
               cost and drawn in red it was read as a loss -- a day with one
               -$7.05 losing trade looked like "over -$14 of losses", which was
               the day's total spread on EIGHT trades, seven of them winners. */
            { key: 'cost', label: 'spread paid', kind: 'bar', tone: 'neutral',
              pick: (r) => -(r.cost_usd ?? 0),
              hint: 'what it cost to trade that day — not money lost' },
          ]}
          detail={(r, i, all) => {
            const run = all.slice(0, i + 1).reduce((t, x) => t + (x.net_usd || 0), 0)
            return [
              ['trades', String(r.trades_closed ?? 0)],
              ['gross', fmt.usd(r.gross_usd), (r.gross_usd ?? 0) >= 0 ? 'pos' : 'neg'],
              ['spread paid', fmt.usd(r.cost_usd)],
              ['net', fmt.usd(r.net_usd), (r.net_usd ?? 0) >= 0 ? 'pos' : 'neg'],
              ['running total', fmt.usd(run), run >= 0 ? 'pos' : 'neg'],
            ]
          }} />
        <div className="mut" style={{ fontSize: 11, marginTop: 4 }}>
          over these {tot.days ?? 0} days, {tot.trades_closed ?? 0} trades made
          {' '}<b className={(tot.gross_usd ?? 0) >= 0 ? 'pos' : 'neg'}>{fmt.usd(tot.gross_usd)}</b>
          {' '}on the coins and paid <b>{fmt.usd(tot.cost_usd)}</b> in spread to do it,
          leaving <b className={(tot.net_usd ?? 0) >= 0 ? 'pos' : 'neg'}>{fmt.usd(tot.net_usd)}</b>.
          <Info text={'Spread is what it COSTS to trade, not money lost on a trade. A day can '
            + 'pay $14 of spread and still be up, and usually is. To see what was actually '
            + 'lost, look for red net figures in the table below or in the Journal.'} />
        </div>
        <div style={{ marginTop: 8 }}>
          <Table
            cols={[
              { key: 'day', label: 'day' },
              { key: 'workable', label: 'workable', num: true },
              { key: 'uncovered_workable', label: 'unwatched', num: true },
              { key: 'missed', label: 'seen, not taken', num: true },
              { key: 'trades_closed', label: 'trades', num: true },
              { key: 'gross_usd', label: 'gross', num: true,
                render: (r) => <span className={(r.gross_usd ?? 0) >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.gross_usd)}</span> },
              { key: 'cost_usd', label: 'spread paid', num: true,
                info: 'What that day\'s trades paid in spread. This is the cost of '
                    + 'trading, NOT a loss — it is already subtracted from net.',
                render: (r) => <span className="mut">{fmt.usd(r.cost_usd)}</span> },
              { key: 'net_usd', label: 'net', num: true,
                render: (r) => <span className={r.net_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_usd)}</span> },
              { key: 'best_available_pct', label: 'best move', num: true,
                render: (r) => `${(r.best_available_pct ?? 0).toFixed(1)}%` },
            ]}
            rows={[...rows].reverse()}
            empty="no reports stored yet — they build on the next scheduler pass"
          />
        </div>
      </Card>
    </>
  )
}
