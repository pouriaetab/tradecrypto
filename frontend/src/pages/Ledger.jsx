import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'
import { BarChart, LineChart, SERIES } from '../components/charts.jsx'

/* ZEC trades near 1,133 and BONK near 0.00000317, so a fixed 6 decimals is wrong
   at both ends. Scale precision to the value. */
const pxFmt = (v) => {
  if (v == null || !isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1000) return v.toFixed(2)
  if (a >= 1) return v.toFixed(4)
  if (a >= 0.01) return v.toFixed(5)
  if (a >= 0.0001) return v.toFixed(7)
  return v.toPrecision(4)
}

const DAY_MS = 86400000
const iso = (d) => new Date(d).toISOString().slice(0, 10)
const toTs = (s) => (s ? new Date(`${s}T00:00:00`).getTime() / 1000 : null)

export default function Ledger({ onModel }) {
  const [mode, setMode] = useState('paper')
  const [from, setFrom] = useState(iso(Date.now() - 30 * DAY_MS))
  const [to, setTo] = useState(iso(Date.now() + DAY_MS))
  const [strategy, setStrategy] = useState(null)

  const range = [mode, from, to]
  const cmp = useAsync(() => api.ledgerCompare(mode, toTs(from), toTs(to)), range, 20000)
  const daily = useAsync(() => api.ledgerDaily(mode, 60), [mode], 20000)
  const one = useAsync(
    () => (strategy ? api.ledger(strategy, mode, toTs(from), toTs(to)) : Promise.resolve(null)),
    [strategy, ...range])

  const rows = cmp.data?.rows || []

  // One fixed colour per strategy, assigned by name order and never cycled,
  // so filtering the list does not repaint the survivors.
  const colorOf = (name) => {
    const names = rows.map((r) => r.strategy).sort()
    return SERIES[names.indexOf(name) % SERIES.length]
  }

  const equitySeries = one.data?.equity_series?.length
    ? [{
        name: strategy,
        color: colorOf(strategy),
        points: one.data.equity_series.map((p) => ({ x: p.ts * 1000, y: p.cumulative_net_usd })),
      }]
    : []

  const dailyBars = (daily.data?.days || []).map((d) => ({
    label: d.day, value: d.net_usd,
    detail: `${d.trades} trades · gross ${fmt.usd(d.gross_usd)} · cost ${fmt.usd(d.cost_usd)}`,
  }))

  return (
    <>
      <h1>Ledger</h1>
      <p className="sub">
        Each strategy keeps its own book. Days run midnight to 11:59pm your time.<Info text="A combined equity curve hides the case where one strategy is quietly paying for another's losses — which is exactly when you want to defund something." />
      </p>

      <div className="filters">
        <label>book</label>
        <div className="seg">
          <button className={mode === 'paper' ? 'on' : ''} onClick={() => setMode('paper')}>paper</button>
          <button className={mode === 'advisory' ? 'on' : ''} onClick={() => setMode('advisory')}>advisory</button>
          <button className={mode === 'mcp' ? 'on' : ''} onClick={() => setMode('mcp')}>live</button>
        </div>
        <label style={{ marginLeft: 12 }}>from</label>
        <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        <label>to</label>
        <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        <div className="spacer" />
        <button onClick={() => { cmp.reload(); daily.reload(); one.reload() }}>refresh</button>
      </div>

      {rows.length === 0 && (
        <Banner kind="info" title={`No closed trades in the ${mode} book for this range`}>
          In paper mode the engine has to be running <i>and</i> a strategy has to be claiming an
          edge before anything lands here. All four currently claim none, which is why the book
          is empty — that is the gate working, not a bug.
        </Banner>
      )}

      <h2>Strategies side by side</h2>
      <Card right={<button className="why" onClick={() => onModel('edge_posterior')}>how P&amp;L becomes an allocation</button>}>
        <Table
          cols={[
            { key: 'strategy', label: 'strategy',
              render: (r) => (
                <span className="row" style={{ gap: 6 }}>
                  <span className="legend-swatch" style={{ background: colorOf(r.strategy) }} />
                  <button className="why" style={{ border: 'none', padding: 0 }}
                          onClick={() => setStrategy(r.strategy)}>{r.strategy}</button>
                </span>) },
            { key: 'trades', label: 'trades', num: true },
            { key: 'net_usd', label: 'net', num: true,
              render: (r) => <span className={r.net_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_usd)}</span> },
            { key: 'gross_usd', label: 'gross', num: true, render: (r) => fmt.usd(r.gross_usd) },
            { key: 'cost_usd', label: 'cost paid', num: true, render: (r) => fmt.usd(r.cost_usd) },
            { key: 'hit_rate', label: 'hit', num: true, render: (r) => fmt.pct(r.hit_rate * 100, 1) },
            { key: 'mean_bps', label: 'mean/trade', num: true, render: (r) => fmt.bps(r.mean_bps, 1) },
            { key: 'positive_at_95', label: 'real edge?',
              render: (r) => <span className={`pill ${r.positive_at_95 ? 'ok' : ''}`}>
                {r.positive_at_95 ? 'yes @95%' : 'not shown'}</span> },
            { key: 'median_hold_min', label: 'median hold', num: true,
              render: (r) => `${fmt.num(r.median_hold_min, 0)}m` },
          ]}
          rows={rows} empty="nothing closed yet" />
        {cmp.data && <div className="mut" style={{ marginTop: 8, fontSize: 12 }}>{cmp.data.note}</div>}
      </Card>

      <h2>Daily net P&amp;L — all strategies</h2>
      <Card>
        <BarChart bars={dailyBars} />
        {daily.data?.summary && (
          <div className="grid c4" style={{ marginTop: 12 }}>
            <Stat label="Days traded" value={daily.data.summary.days_traded} small />
            <Stat label="Total net" small value={fmt.usd(daily.data.summary.total_net_usd)}
                  tone={(daily.data.summary.total_net_usd ?? 0) >= 0 ? 'pos' : 'neg'} />
            <Stat label="Green days" small
                  value={`${daily.data.summary.green_days} / ${daily.data.summary.days_traded}`} />
            <Stat label="Average day" small value={fmt.usd(daily.data.summary.avg_day_usd)} />
          </div>
        )}
      </Card>

      {strategy && (
        <>
          <h2>{strategy} — full book</h2>
          {one.data?.n_trades === 0 ? (
            <Card><div className="mut">{one.data.note}</div></Card>
          ) : one.data && (
            <>
              <div className="grid c4">
                <Stat label="Net" value={fmt.usd(one.data.totals.net_usd)} small
                      tone={one.data.totals.net_usd >= 0 ? 'pos' : 'neg'} />
                <Stat label="Gross" value={fmt.usd(one.data.totals.gross_usd)} small />
                <Stat label="Cost paid" value={fmt.usd(one.data.totals.cost_usd)} small
                      model="execution_cost" onModel={onModel}
                      meta={one.data.totals.cost_share_of_gross != null
                        ? `${fmt.pct(one.data.totals.cost_share_of_gross * 100)} of gross` : ''} />
                <Stat label="Mean net / trade" n={one.data.n_trades}
                      value={fmt.bps(one.data.performance?.mean_return_bps, 1)}
                      tone={(one.data.performance?.mean_return_bps ?? 0) >= 0 ? 'pos' : 'neg'} />
              </div>

              <Card title="cumulative net, trade by trade">
                <LineChart series={equitySeries} xFormat={(x) => new Date(x).toLocaleString()} />
              </Card>

              <Card title="by day">
                <Table
                  cols={[
                    { key: 'day', label: 'day' },
                    { key: 'trades', label: 'trades', num: true },
                    { key: 'gross_usd', label: 'gross', num: true, render: (r) => fmt.usd(r.gross_usd) },
                    { key: 'cost_usd', label: 'cost', num: true, render: (r) => fmt.usd(r.cost_usd) },
                    { key: 'net_usd', label: 'net', num: true,
                      render: (r) => <span className={r.net_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_usd)}</span> },
                    { key: 'hit_rate', label: 'hit', num: true, render: (r) => fmt.pct(r.hit_rate * 100, 0) },
                  ]}
                  rows={one.data.daily} />
              </Card>

              <Card title="by coin">
                <Table
                  cols={[
                    { key: 'symbol', label: 'coin' },
                    { key: 'trades', label: 'trades', num: true },
                    { key: 'net_usd', label: 'net', num: true,
                      render: (r) => <span className={r.net_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_usd)}</span> },
                    { key: 'hit_rate', label: 'hit', num: true, render: (r) => fmt.pct(r.hit_rate * 100, 0) },
                  ]}
                  rows={one.data.by_symbol} />
              </Card>

              <Card title="trades">
                <Table
                  cols={[
                    { key: 'ts_close', label: 'closed', render: (r) => fmt.time(r.ts_close) },
                    { key: 'symbol', label: 'coin' },
                    { key: 'entry_px', label: 'bought at', num: true,
                      render: (r) => <span className="mono">{pxFmt(r.entry_px)}</span> },
                    { key: 'exit_px', label: 'sold at', num: true,
                      render: (r) => <span className="mono">{pxFmt(r.exit_px)}</span> },
                    { key: 'holding_s', label: 'held', num: true,
                      // 14-hour holds were rendering as "840m".
                      render: (r) => (r.holding_s >= 3600
                        ? `${(r.holding_s / 3600).toFixed(1)}h` : `${(r.holding_s / 60).toFixed(0)}m`) },
                    { key: 'move_pct', label: 'move', num: true,
                      render: (r) => {
                        if (!r.entry_px || !r.exit_px) return '—'
                        const m = (r.exit_px / r.entry_px - 1) * 100
                        return <span className={m >= 0 ? 'pos' : 'neg'}>{m >= 0 ? '+' : ''}{m.toFixed(2)}%</span>
                      } },
                    { key: 'gross_pnl_usd', label: 'coin moved', num: true, render: (r) => fmt.usd(r.gross_pnl_usd) },
                    { key: 'cost_usd', label: 'cost', num: true, render: (r) => fmt.usd(r.cost_usd) },
                    { key: 'net_pnl_usd', label: 'net', num: true,
                      render: (r) => <span className={r.net_pnl_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_pnl_usd)}</span> },
                  ]}
                  rows={one.data.trades} />
              </Card>
            </>
          )}
        </>
      )}
    </>
  )
}
