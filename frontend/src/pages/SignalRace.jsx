import React from 'react'
import { api } from '../lib/api.js'
import { Banner, Card, Table, useAsync } from '../components/ui.jsx'

/** Every signal that competed for a slot, and the formula that ranked them.
 *
 * 2026-09-23, the operator: "for every signal that become a buy or sell order i
 * want ... to see what were there all the signals for that group ... competing
 * to become a order ... and behind each every decision i want to know what
 * factors/variable were include and with what weights and what algorithm."
 *
 * A race is one strategy in one bar. Two strategies looking at the same second
 * are two races -- they never ranked against each other, and merging them would
 * invent a competition that did not happen.
 */
export default function SignalRace() {
  const r = useAsync(() => api.signalRaces(), [], 60000)
  const [open, setOpen] = React.useState(null)
  const [who, setWho] = React.useState(null)

  if (r.err) return <Banner tone="neg">{r.err}</Banner>
  if (!r.data) return <div className="mut">loading…</div>
  const races = r.data.races || []

  return (
    <>
      <h1>Signals</h1>
      <p className="mut" style={{ marginTop: -6 }}>
        Each row is one strategy looking at one moment. It shows every coin that
        strategy considered, the score each got, which one won, and why the rest
        did not. Click a row to open the field and the scoring formula.
      </p>

      {!races.length && (
        <Card><div className="mut">No races recorded yet.</div></Card>
      )}

      <Card>
        <Table
          rows={races.map((x, i) => ({ ...x, _i: i }))}
          cols={[
            { key: 'ts', label: 'when',
              render: (x) => new Date(x.ts * 1000).toLocaleString() },
            { key: 'strategy', label: 'strategy' },
            { key: 'candidates', label: 'coins it looked at', num: true,
              render: (x) => (x.lone_candidate
                ? <span style={{ color: 'var(--warning)' }} title={
                    'Only one coin qualified. Measured: these returned -3.11% a trade '
                    + 'against +0.57% when two or more did (14 vs 58 trades, p=0.007). '
                    + 'Now refused at entry.'}>1 — alone</span>
                : x.candidates) },
            { key: 'winners', label: 'promoted to an order',
              render: (x) => (x.winners || []).join(', ') || '—' },
            { key: 'winner_rank', label: 'winner ranked', num: true,
              render: (x) => (x.winner_rank == null ? '—'
                : `#${x.winner_rank} of ${x.candidates}`) },
            { key: 'top_score', label: 'top score', num: true,
              render: (x) => (x.top_score == null ? '—' : Number(x.top_score).toFixed(3)) },
            { key: '_open', label: '',
              render: (x) => (
                <button className="btn" onClick={() => setOpen(open === x._i ? null : x._i)}>
                  {open === x._i ? 'hide' : 'show the field'}
                </button>) },
          ]}
        />
      </Card>

      {open != null && races[open] && (() => {
        const race = races[open]
        const rec = race.recipe || {}
        const featKeys = [...new Set(race.field.flatMap((f) => Object.keys(f.features || {})))]
          .filter((k) => typeof (race.field.find((f) => f.features?.[k] != null)?.features?.[k]) === 'number')
          .slice(0, 8)
        return (
          <>
            <h2>How {race.strategy} ranked them</h2>
            <Card>
              {rec.known ? (
                <>
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 13,
                                padding: '6px 10px', background: 'var(--surface-2)',
                                borderRadius: 4, display: 'inline-block', marginBottom: 8 }}>
                    {rec.expr}
                  </div>
                  <div className="mut" style={{ fontSize: 12, marginBottom: 10 }}>
                    {rec.plain} <br />Source: {rec.source}
                  </div>
                  <Table
                    rows={rec.inputs || []}
                    cols={[
                      { key: 'name', label: 'variable' },
                      { key: 'weight', label: 'weight' },
                      { key: 'what', label: 'what it measures' },
                    ]}
                  />
                  {!!(rec.gates_before_scoring || []).length && (
                    <div className="mut" style={{ fontSize: 12, marginTop: 10 }}>
                      <b>Pass/fail before any scoring happens</b> — these decide who is
                      eligible, not who wins:
                      <ul style={{ margin: '4px 0 0 18px' }}>
                        {rec.gates_before_scoring.map((g, i) => <li key={i}>{g}</li>)}
                      </ul>
                    </div>
                  )}
                </>
              ) : (
                <Banner tone="warn">{rec.plain}</Banner>
              )}
            </Card>

            <h2>The field</h2>
            <Card>
              <Table
                rows={race.field}
                cols={[
                  { key: 'rank', label: '#', num: true },
                  { key: 'symbol', label: 'coin' },
                  { key: 'raw_score', label: 'score', num: true,
                    render: (x) => Number(x.raw_score ?? 0).toFixed(4) },
                  { key: 'won', label: 'outcome',
                    render: (x) => (x.won
                      ? <span style={{ color: 'var(--pos)' }}>became an order</span>
                      : <span className="mut">not taken</span>) },
                  { key: 'reject_reason', label: 'why not',
                    render: (x) => x.reject_reason || (x.won ? '—' : 'lost on score') },
                  { key: '_gates', label: 'every gate',
                    value: (x) => (x.checks || []).length,
                    render: (x) => (!(x.checks || []).length
                      ? <span className="mut">not recorded</span>
                      : <button className="btn" onClick={() => setWho(
                          who === x.symbol ? null : x.symbol)}>
                          {who === x.symbol ? 'hide' : `${x.checks.length} checks`}
                        </button>) },
                  { key: 'expected_edge_bps', label: 'expected edge (bps)', num: true,
                    render: (x) => Number(x.expected_edge_bps ?? 0).toFixed(1) },
                  { key: 'cost_hurdle_bps', label: 'cost to beat (bps)', num: true,
                    render: (x) => Number(x.cost_hurdle_bps ?? 0).toFixed(1) },
                  { key: 'sample_size', label: 'trades behind that edge', num: true },
                  ...featKeys.map((k) => ({
                    key: `f_${k}`, label: k.replace(/_/g, ' '), num: true,
                    value: (x) => x.features?.[k],
                    render: (x) => (x.features?.[k] == null ? '—'
                      : Number(x.features[k]).toFixed(3)),
                  })),
                ]}
              />
            </Card>

            {who && (() => {
              const cand = race.field.find((f) => f.symbol === who)
              if (!cand || !(cand.checks || []).length) return null
              return (
                <>
                  <h2>Every gate that judged {who}</h2>
                  <Card>
                    <div className="mut" style={{ fontSize: 11, marginBottom: 8 }}>
                      These run in order and the first failure stops the trade. The
                      limit is the threshold in force at that moment; the reading is
                      what this signal actually measured.
                    </div>
                    <Table
                      rows={cand.checks.map((c, i) => ({ ...c, _n: i + 1 }))}
                      cols={[
                        { key: '_n', label: '#', num: true },
                        { key: 'check', label: 'gate' },
                        { key: 'passed', label: 'result',
                          render: (c) => (c.passed
                            ? <span style={{ color: 'var(--pos)' }}>passed</span>
                            : <span style={{ color: 'var(--neg)' }}>BLOCKED</span>) },
                        { key: 'limit', label: 'limit', num: true,
                          render: (c) => (c.limit == null ? '—' : String(c.limit)) },
                        { key: 'actual', label: 'reading', num: true,
                          render: (c) => (c.actual == null ? '—' : String(c.actual)) },
                        { key: 'detail', label: 'what it means',
                          render: (c) => <span className="mut">{c.detail}</span> },
                      ]}
                    />
                  </Card>
                </>
              )
            })()}
          </>
        )
      })()}
    </>
  )
}
