import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

export default function CostLab({ onModel }) {
  const bd = useAsync(() => api.costBreakdown(), [], 20000)
  const per = useAsync(() => api.costSymbols(), [], 60000)
  const [form, setForm] = useState({ symbol: 'DOGE', side: 'buy', notional_usd: 25, mid_at_submit: '', fill_px: '' })
  const [msg, setMsg] = useState(null)

  const submit = async (e) => {
    e.preventDefault()
    try {
      const d = await api.addCostObs({
        symbol: form.symbol.toUpperCase(), side: form.side,
        notional_usd: Number(form.notional_usd),
        mid_at_submit: Number(form.mid_at_submit), fill_px: Number(form.fill_px),
      })
      setMsg(`recorded: ${fmt.bps(d.half_spread_bps, 1)} adverse on this fill. New estimate: ${fmt.bps(d.new_estimate.per_side_bps, 1)} per side (${d.new_estimate.source}).`)
      bd.reload()
    } catch (err) { setMsg(err.message) }
  }

  const e = bd.data?.current_estimate
  const hs = bd.data?.effective_half_spread
  const isf = bd.data?.implementation_shortfall

  return (
    <>
      <h1>Cost Lab</h1>
      <p className="sub">
        Execution cost decides whether anything else here is worth building.<Info text="On a $500 account trading a spread-cost venue, execution cost is usually larger than any edge you can find. Your 0.245 -> 0.2475 observation is one coin on one day; the per-coin table turns that anecdote into an estimate, and every real fill you log moves a coin from estimate to measurement." />
      </p>

      {e && (
        <Banner kind={e.source === 'prior' ? 'warn' : 'info'} title={`Current basis: ${e.source}`}>
          {e.plain_english}
        </Banner>
      )}

      <div className="grid c4">
        <Stat label="Cost per side" value={e ? fmt.bps(e.per_side_bps, 1) : '—'} meta={e?.source}
              model="execution_cost" onModel={onModel} />
        <Stat label="Round trip" value={e ? fmt.pct(e.round_trip_bps / 100) : '—'} />
        <Stat label="Required gross edge" value={e ? fmt.pct(e.hurdle_bps / 100) : '—'}
              meta="nothing trades below this" />
        <Stat label="Real fills recorded" value={e?.n_observations ?? 0} small />
      </div>

      <h2>Measured components</h2>
      <div className="grid c3">
        <Stat label="Effective half-spread" n={hs?.n} minN={20}
              value={hs?.value != null ? fmt.bps(hs.value, 1) : '—'}
              meta={hs?.ci_low != null ? `CI [${fmt.num(hs.ci_low, 0)}, ${fmt.num(hs.ci_high, 0)}]` : hs?.method}
              model="execution_cost" onModel={onModel} />
        <Stat label="Implementation shortfall" n={isf?.n} minN={20}
              value={isf?.value != null ? fmt.bps(isf.value, 1) : '—'}
              meta="fill vs price at decision (Perold 1988)" model="execution_cost" onModel={onModel} />
        <Stat label="Delay component" small
              value={bd.data?.delay_component_bps != null ? fmt.bps(bd.data.delay_component_bps, 1) : '—'}
              meta="how much the market moved while we were deciding" />
      </div>

      <h2>Log a real fill<Info text="Do this after any manual Robinhood trade. Displayed price is what you saw when you tapped; fill price is what you actually got. Twenty of these and the prior is gone." /></h2>
      <Card>
        <form onSubmit={submit} className="row">
          <input value={form.symbol} onChange={(ev) => setForm({ ...form, symbol: ev.target.value })} placeholder="symbol" style={{ width: 90 }} />
          <select value={form.side} onChange={(ev) => setForm({ ...form, side: ev.target.value })}>
            <option value="buy">buy</option><option value="sell">sell</option>
          </select>
          <input value={form.notional_usd} onChange={(ev) => setForm({ ...form, notional_usd: ev.target.value })} placeholder="$ size" style={{ width: 90 }} />
          <input value={form.mid_at_submit} onChange={(ev) => setForm({ ...form, mid_at_submit: ev.target.value })} placeholder="displayed price" style={{ width: 140 }} />
          <input value={form.fill_px} onChange={(ev) => setForm({ ...form, fill_px: ev.target.value })} placeholder="fill price" style={{ width: 120 }} />
          <button className="primary" type="submit">record</button>
        </form>
        {msg && <div style={{ marginTop: 10 }} className="mono">{msg}</div>}
      </Card>

      <h2>Estimated cost per coin</h2>
      {per.data && (
        <Banner kind={per.data.coefficients.fitted ? 'info' : 'warn'}
                title={per.data.coefficients.fitted
                  ? `Elasticities fitted on ${per.data.coefficients.n_coins} coins (R² ${fmt.num(per.data.coefficients.r_squared, 2)})`
                  : 'Elasticities are still documented priors, not fitted'}>
          {per.data.coefficients.note} {per.data.caveat}
        </Banner>
      )}
      <Card right={<button className="why" onClick={() => onModel('symbol_cost_estimator')}>how this is estimated</button>}>
        <Table
          cols={[
            { key: 'symbol', label: 'coin' },
            { key: 'round_trip_pct', label: 'est. round trip', num: true,
              render: (r) => <span className={r.round_trip_bps > 150 ? 'neg' : 'pos'}>{fmt.pct(r.round_trip_pct)}</span> },
            { key: 'ci', label: '95% range', num: true,
              render: (r) => `${fmt.pct(r.ci_low_bps / 100)} – ${fmt.pct(r.ci_high_bps / 100)}` },
            { key: 'hurdle_pct', label: 'gross edge needed', num: true, render: (r) => fmt.pct(r.hurdle_pct) },
            { key: 'status', label: 'basis',
              render: (r) => <span className={`pill ${r.status === 'measured' ? 'ok' : r.status === 'assumed' ? 'bad' : 'warn'}`}>{r.status}</span> },
            { key: 'n_fills', label: 'real fills', num: true },
            { key: 'drivers', label: 'what makes it expensive',
              render: (r) => <span className="mut">{Object.keys(r.drivers || {}).filter((k) => k !== 'shrinkage').join(', ') || '—'}</span> },
          ]}
          rows={per.data?.rows || []}
          empty="run the engine once so the estimator has quotes and bars to work from" />
      </Card>

      <h2>Measured cost by symbol (real fills only)</h2>
      <Card>
        <Table
          cols={[
            { key: 'symbol', label: 'symbol' },
            { key: 'n', label: 'fills', num: true },
            { key: 'mean_half_bps', label: 'half-spread', num: true, render: (r) => fmt.bps(r.mean_half_bps, 1) },
            { key: 'mean_shortfall_bps', label: 'shortfall', num: true, render: (r) => fmt.bps(r.mean_shortfall_bps, 1) },
            { key: 'rt', label: 'round trip', num: true, render: (r) => fmt.pct((r.mean_half_bps * 2) / 100) },
          ]}
          rows={bd.data?.per_symbol || []}
          empty="no measured fills yet — the table fills in as you log them"
        />
      </Card>
    </>
  )
}
