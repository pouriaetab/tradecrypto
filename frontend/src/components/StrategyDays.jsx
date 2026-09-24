import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Card, Info, Table, useAsync } from './ui.jsx'

/* The strategy-level view of the Daily tab.
 *
 *   "how successful or not it was and how daily it was improved ... what
 *    change of direction was done ... how this strategy fares against all
 *    other ones on day to day and overall"
 *
 * One league table for the period, then one collapsible block per strategy
 * with its day-by-day rows, what the desk recorded changing about it each
 * day, its exit-lab verdict and its posterior. Every number here is read
 * from research/strategy_days.py, which reads the tables the desk writes.
 * Nothing on this page is estimated that was not counted. */

const pct = (v, d = 2) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(d)}%`)
const usd = (v) => (v == null ? '—' : `${v >= 0 ? '+' : '-'}$${Math.abs(Number(v)).toFixed(2)}`)
const tone = (v) => (v == null ? 'mut' : v > 0 ? 'pos' : v < 0 ? 'neg' : 'mut')

function Verdict({ v }) {
  if (!v) return <span className="mut">—</span>
  const good = /better/.test(v.verdict), bad = /worse/.test(v.verdict)
  return (
    <span className={good ? 'pos' : bad ? 'neg' : 'mut'} title={v.t != null ? `Welch t = ${v.t.toFixed(2)}, diff ${pct(v.diff_pct)}` : ''}>
      {v.verdict}{v.t != null ? ` (${v.t >= 0 ? '+' : ''}${v.t.toFixed(1)}σ)` : ''}
    </span>
  )
}

function Changes({ list }) {
  const [open, setOpen] = useState(false)
  if (!list || !list.length) return <span className="mut">none recorded</span>
  return (
    <span>
      <button type="button" className="linkish" onClick={() => setOpen((o) => !o)}>
        {list.length} change{list.length === 1 ? '' : 's'}{list.some((c) => c.promoted) ? ' · PROMOTED' : ''} {open ? '▾' : '▸'}
      </button>
      {open && (
        <div style={{ marginTop: 4 }}>
          {list.map((c, i) => (
            <div key={i} style={{ fontSize: 11, marginBottom: 2 }}>
              <span className={`pill ${c.promoted ? 'ok' : c.kind === 'relearn' ? 'warn' : ''}`} style={{ marginRight: 6 }}>{c.kind}</span>
              <span className="mut">{new Date(c.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>{' '}
              {c.text}{c.detail ? <span className="mut"> — {c.detail}</span> : null}
            </div>
          ))}
        </div>
      )}
    </span>
  )
}

function StrategyBlock({ name, s, days, selectedDay }) {
  const o = s.overall
  const todayRow = s.days.find((r) => r.day === selectedDay)
  const rows = [...s.days].reverse()
  return (
    <Card collapse={`daily.strategy.${name}`}
          title={`${name}${s.in_roster ? '' : ' (not in roster)'} — ${o.n} trade${o.n === 1 ? '' : 's'}, ${usd(o.net_usd)} over ${days.length} days`}>
      <div className="grid c4" style={{ marginBottom: 8 }}>
        <div>
          <div className="label mut">the selected day ({selectedDay})</div>
          {todayRow && todayRow.n
            ? <div><b className={tone(todayRow.net_usd)}>{usd(todayRow.net_usd)}</b> · {todayRow.n} trade{todayRow.n === 1 ? '' : 's'} · {pct(todayRow.mean_net_pct)}/trade
                {todayRow.rank_usd && <span className="mut"> · #{todayRow.rank_usd} of {todayRow.peers_that_day} by $, #{todayRow.rank_pct} by %</span>}
                <div style={{ fontSize: 11 }}><Verdict v={todayRow.vs_earlier} /></div></div>
            : <div className="mut">no trades that day{todayRow ? ` · ${todayRow.funnel.signals} signals, ${todayRow.funnel.taken} taken` : ''}</div>}
        </div>
        <div>
          <div className="label mut">the period</div>
          <div>{pct(o.mean_net_pct)}/trade{o.se_pct != null ? ` ± ${o.se_pct.toFixed(2)}` : ''} · win {o.win_rate == null ? '—' : `${o.win_rate.toFixed(0)}%`}
            {o.hit_rate_ci90 && <span className="mut"> (90% CI {o.hit_rate_ci90[0].toFixed(0)}–{o.hit_rate_ci90[1].toFixed(0)}%)</span>}</div>
          <div style={{ fontSize: 11 }} className="mut">{o.days_positive}/{o.days_traded} traded days positive · max drawdown {usd(o.max_drawdown_usd)} · profit factor {o.profit_factor == null ? '—' : o.profit_factor === Infinity ? '∞' : o.profit_factor.toFixed(2)}</div>
        </div>
        <div>
          <div className="label mut">is it improving?<Info text="The second half of this strategy's trades in the period against the first half, Welch's t. Under two standard errors the honest answer is 'not distinguishable' — with a handful of trades per day that will be the answer most days, and printing a direction anyway would be fitting to noise." /></div>
          <div style={{ fontSize: 12 }}><Verdict v={o.first_half_vs_second_half} /></div>
          <div style={{ fontSize: 11 }} className="mut">{o.changes} recorded change{o.changes === 1 ? '' : 's'}{o.promotions ? `, ${o.promotions} promotion${o.promotions === 1 ? '' : 's'}` : ', nothing promoted'}</div>
        </div>
        <div>
          <div className="label mut">belief and exits</div>
          {s.posterior
            ? <div style={{ fontSize: 12 }}>P(edge &gt; cost) {s.posterior.p_edge_above_hurdle == null ? '—' : s.posterior.p_edge_above_hurdle.toFixed(2)} · {s.posterior.status}
                <div className="mut" style={{ fontSize: 11 }}>{s.posterior.reason}</div></div>
            : <div className="mut">no posterior yet</div>}
          {s.exit_lab && s.exit_lab.n_trades
            ? <div style={{ fontSize: 11 }} className="mut">exit lab: as traded {pct(s.exit_lab.as_traded_pct)}; best other rule {s.exit_lab.best_rule} {pct(s.exit_lab.best_rule_pct)} ({s.exit_lab.best_rule_verdict}); avg given back {s.exit_lab.giveback_pts == null ? '—' : `${s.exit_lab.giveback_pts.toFixed(2)} pts`}</div>
            : null}
        </div>
      </div>
      <Table
        cols={[
          { key: 'day', label: 'day', render: (r) => <span className={r.day === selectedDay ? 'pos' : ''}>{r.day.slice(5)}</span> },
          { key: 'n', label: 'trades', num: true, render: (r) => (r.n ? r.n : <span className="mut">0</span>) },
          { key: 'net_usd', label: 'net $', num: true, render: (r) => (r.n ? <span className={tone(r.net_usd)}>{usd(r.net_usd)}</span> : <span className="mut">—</span>) },
          { key: 'cum_net_usd', label: 'cumulative', num: true, render: (r) => <span className={tone(r.cum_net_usd)}>{usd(r.cum_net_usd)}</span> },
          { key: 'mean_net_pct', label: 'net %/trade', num: true, render: (r) => (r.n ? <span className={tone(r.mean_net_pct)}>{pct(r.mean_net_pct)}</span> : '—') },
          { key: 'win_rate', label: 'win', num: true, render: (r) => (r.n ? `${r.win_rate.toFixed(0)}%` : '—') },
          { key: 'median_hold_h', label: 'hold', num: true, render: (r) => (r.median_hold_h == null ? '—' : `${r.median_hold_h.toFixed(1)}h`) },
          { key: 'rank_usd', label: 'rank', num: true,
            info: 'Among the strategies that traded that day: by dollars, then by per-trade percent. A strategy that trades nine times a day wins the dollar column on volume alone — that is why both are shown.',
            render: (r) => (r.rank_usd ? `#${r.rank_usd}/${r.peers_that_day} $ · #${r.rank_pct} %` : '—') },
          { key: 'funnel', label: 'signals → taken', num: true, render: (r) => `${r.funnel.signals} → ${r.funnel.taken}` },
          { key: 'vs_earlier', label: 'vs its earlier trades', render: (r) => (r.n ? <Verdict v={r.vs_earlier} /> : <span className="mut">—</span>) },
          { key: 'changes', label: 'what changed', render: (r) => <Changes list={r.changes} /> },
        ]}
        rows={rows} empty="nothing in this window" />
    </Card>
  )
}

export default function StrategyDays({ start, end, selectedDay }) {
  const q = useAsync(() => api.strategyDays(start, end), [start, end], 300000)
  const [view, setView] = useState('league')
  if (q.err) return <Card title="strategies — day by day"><div className="neg">{q.err}</div></Card>
  const d = q.data
  if (!d) return <Card title="strategies — day by day"><div className="mut">counting…</div></Card>
  const names = d.league.map((l) => l.strategy)
  return (
    <>
      <Card collapse="daily.strategies-league" title={`strategies — the league over ${d.days.length} days (desk ${usd(d.desk.net_usd)}, ${d.desk.trades} trades)`}>
        <div className="filters">
          <div className="seg">
            <button className={view === 'league' ? 'on' : ''} onClick={() => setView('league')}>period table</button>
            <button className={view === 'grid' ? 'on' : ''} onClick={() => setView('grid')}>day-by-day grid</button>
          </div>
          <span className="mut" style={{ fontSize: 11 }}>{d.how_to_read}</span>
        </div>
        {view === 'league' ? (
          <Table
            cols={[
              { key: 'rank_usd', label: '#', num: true },
              { key: 'strategy', label: 'strategy', render: (r) => <span>{r.strategy}{!r.in_roster && <span className="mut"> (retired)</span>}</span> },
              { key: 'n', label: 'trades', num: true },
              { key: 'net_usd', label: 'net $', num: true, render: (r) => <span className={tone(r.net_usd)}>{usd(r.net_usd)}</span> },
              { key: 'share_of_desk_pct', label: 'of desk', num: true, render: (r) => (r.share_of_desk_pct == null ? '—' : `${r.share_of_desk_pct.toFixed(0)}%`) },
              { key: 'mean_net_pct', label: 'net %/trade', num: true, render: (r) => (r.mean_net_pct == null ? '—' : <span className={tone(r.mean_net_pct)}>{pct(r.mean_net_pct)}{r.se_pct != null ? <span className="mut"> ±{r.se_pct.toFixed(2)}</span> : null}</span>) },
              { key: 'rank_pct', label: '# by %', num: true },
              { key: 'win_rate', label: 'win', num: true, render: (r) => (r.win_rate == null ? '—' : `${r.win_rate.toFixed(0)}%`) },
              { key: 'days_traded', label: 'days +/traded', num: true, render: (r) => `${r.days_positive}/${r.days_traded}` },
              { key: 'max_drawdown_usd', label: 'max DD', num: true, render: (r) => <span className={r.max_drawdown_usd < 0 ? 'neg' : 'mut'}>{usd(r.max_drawdown_usd)}</span> },
              { key: 'profit_factor', label: 'PF', num: true, info: 'Gross gains divided by gross losses, in dollars. Above 1 the winners paid for the losers.',
                render: (r) => (r.profit_factor == null ? '—' : r.profit_factor === Infinity ? '∞' : r.profit_factor.toFixed(2)) },
              { key: 'cost_usd', label: 'spread paid', num: true, render: (r) => `$${Number(r.cost_usd || 0).toFixed(2)}` },
              { key: 'signals', label: 'signals → taken', num: true, render: (r) => `${r.signals} → ${r.taken}` },
              { key: 'p_edge_above_hurdle', label: 'P(edge > cost)', num: true, render: (r) => (r.p_edge_above_hurdle == null ? '—' : r.p_edge_above_hurdle.toFixed(2)) },
              { key: 'changes', label: 'changes', num: true, render: (r) => `${r.changes}${r.promotions ? ` (${r.promotions} promoted)` : ''}` },
            ]}
            rows={d.league} empty="no strategies" />
        ) : (
          <div className="scroll">
            <table>
              <thead>
                <tr>
                  <th>strategy</th>
                  {d.days.map((day) => <th key={day} className="num">{day.slice(5)}</th>)}
                  <th className="num">period</th>
                </tr>
              </thead>
              <tbody>
                {names.map((s) => (
                  <tr key={s}>
                    <td>{s}</td>
                    {d.strategies[s].days.map((r) => (
                      <td key={r.day} className="num" title={r.n ? `${r.n} trades, ${pct(r.mean_net_pct)}/trade, rank #${r.rank_usd} of ${r.peers_that_day}` : 'no trades'}>
                        {r.n ? <span className={tone(r.net_usd)}>{usd(r.net_usd)}<span className="mut" style={{ fontSize: 10 }}> #{r.rank_usd}</span></span> : <span className="mut">·</span>}
                      </td>
                    ))}
                    <td className="num"><b className={tone(d.strategies[s].overall.net_usd)}>{usd(d.strategies[s].overall.net_usd)}</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      {names.map((s) => (
        <StrategyBlock key={s} name={s} s={d.strategies[s]} days={d.days} selectedDay={selectedDay} />
      ))}
    </>
  )
}
