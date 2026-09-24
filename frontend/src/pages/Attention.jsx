import React from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

export default function Attention({ onModel }) {
  const a = useAsync(() => api.attention(), [], 60000)
  const p = useAsync(() => api.attentionPersistence(), [])

  return (
    <>
      <h1>Attention</h1>
      <p className="sub">
        Which coins are in play today, and whose run is already late.<Info text="Attention asks whether unusual activity is happening; exhaustion asks how much of the move is already behind us. High attention with LOW exhaustion is the interesting quadrant. High on both is the trap: you see it because it already ran." />
      </p>

      {a.data?.available === false && (
        <Banner kind="warn" title="Not enough history yet">{a.data.note}</Banner>
      )}

      {a.data?.available && (
        <>
          <div className="grid c3">
            <Stat label="Today's top three" small value={(a.data.top || []).join(', ') || '—'}
                  model="attention_model" onModel={onModel} />
            <Stat label="In play, run not obviously over" small
                  value={(a.data.tradeable_now || []).join(', ') || 'none'} />
            <Stat label="Coins ranked" small value={(a.data.rows || []).length} />
          </div>

          <Card title="ranked by attention">
            <Table
              cols={[
                { key: 'symbol', label: 'coin' },
                { key: 'attention_score', label: 'attention', num: true,
                  render: (r) => <span className={(r.attention_score ?? 0) > 1 ? 'pos' : 'mut'}>
                    {fmt.num(r.attention_score, 2)}</span> },
                { key: 'day_return_pct', label: 'today', num: true,
                  render: (r) => <span className={(r.day_return_pct ?? 0) >= 0 ? 'pos' : 'neg'}>
                    {fmt.pct(r.day_return_pct)}</span> },
                { key: 'rvol', label: 'rel. volume', num: true,
                  render: (r) => fmt.num(r.components?.relative_volume_log, 2) },
                { key: 'rexp', label: 'range vs normal', num: true,
                  render: (r) => `${fmt.num(r.components?.range_expansion, 2)}×` },
                { key: 'exhaustion_score', label: 'exhaustion', num: true,
                  render: (r) => <span className={(r.exhaustion_score ?? 0) > 0.7 ? 'neg' : 'pos'}>
                    {fmt.num(r.exhaustion_score, 2)}</span> },
                { key: 'pos', label: 'where in day range', num: true,
                  render: (r) => fmt.pct((r.exhaustion_components?.position_in_day_range ?? 0) * 100, 0) },
                { key: 'verdict', label: 'verdict',
                  render: (r) => <span className={`pill ${
                    r.verdict?.includes('not obviously over') ? 'ok'
                      : r.verdict?.includes('late') ? 'bad' : ''}`}>{r.verdict}</span> },
              ]}
              rows={a.data.rows || []} />
          </Card>
        </>
      )}

      <h2>Does yesterday's leader lead again?</h2>
      <Card>
        {p.data?.available === false ? <div className="mut">{p.data.note}</div> : p.data && (
          <>
            <div className="grid c4">
              <Stat label="Repeat rate" small value={fmt.pct(p.data.repeat_rate * 100, 0)}
                    meta={`${p.data.days_compared} day pairs`} />
              <Stat label="If it were random" small value={fmt.pct(p.data.rate_if_random * 100, 0)} />
              <Stat label="Lift" small value={`${fmt.num(p.data.lift, 2)}×`}
                    tone={(p.data.lift ?? 0) > 1.5 ? 'pos' : 'neg'} />
              <Stat label="Top-K compared" small value={p.data.top_k} />
            </div>
            <Banner kind={p.data.repeat_rate > p.data.rate_if_random * 1.5 ? 'info' : 'warn'}
                    title={p.data.verdict}>{p.data.caveat}</Banner>
          </>
        )}
      </Card>
    </>
  )
}
