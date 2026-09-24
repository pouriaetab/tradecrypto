import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Card, Info, Table, useAsync } from '../components/ui.jsx'
// The symbol chart used to be opened from here: three copies of the same
// 200-character inline `render`, plus local `pick` state and a <CoinChart/> at
// the bottom of the page. It now lives in components/ui.jsx -- Table turns any
// `symbol` column into a chart button, and one <SymbolChartHost/> at the app
// root renders it -- so every tab has it and no page can get the wiring wrong.
//
// Keeping the original note, because it is the reason the shared version exists:
// this page once rendered <CoinChart/> WITHOUT importing it. `pick` starts null,
// so the reference was never evaluated and the page looked fine; the first click
// threw ReferenceError inside render, React 18 unmounted the entire tree, and the
// app went blank with the nav gone too. DayScan has its own local component of
// the same name, which is why the pattern looked correct when it was copied.

const TABS = ['signals', 'orders', 'trades', 'events']

/* Crypto prices span ZEC at 1,133 and BONK at 0.00000317, so a fixed number of
   decimals is useless at one end or the other. Scale the precision to the value. */
const fmtPx = (v) => {
  if (v == null || !isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1000) return v.toFixed(2)
  if (a >= 1) return v.toFixed(4)
  if (a >= 0.01) return v.toFixed(5)
  if (a >= 0.0001) return v.toFixed(7)
  return v.toPrecision(4)
}

const DAY = 86400000
const iso = (d) => new Date(d).toISOString().slice(0, 10)
const toTs = (s) => (s ? new Date(`${s}T00:00:00`).getTime() / 1000 : undefined)

/* Column explanations. These live here rather than in a paragraph above the
   table so the table stays dense and the meaning is one hover away. */
const WHAT = {
  score: 'The strategy’s own raw conviction, before any money question is asked. '
    + 'Scales differ between strategies, so compare a score to other scores from the '
    + 'SAME strategy, never across them.',
  edge: 'How much the strategy expects to gain on this trade, gross, in basis points '
    + '(1 bp = 0.01%). This is a forecast with error bars, not a promise — it comes '
    + 'from that strategy’s fitted model on past data.',
  hurdle: 'What the trade must beat to be worth taking, for THIS coin. It is '
    + 'Robinhood’s published round-trip spread (~192 bps at 0.95% per side) plus a '
    + 'slippage allowance plus the profit we insist on keeping. If expected edge is '
    + 'below this line, taking the trade loses money on average even when the '
    + 'direction is right.',
  decision: 'taken = an order was placed. rejected = the signal fired but did not '
    + 'clear the hurdle, or a risk guard blocked it. The rejected rows are the more '
    + 'informative half: they show how often cost, not prediction, is the binding '
    + 'constraint.',
  why: 'The specific test that failed. Almost always "expected edge < cost hurdle", '
    + 'which means the strategy was not wrong — it was not right by enough to pay '
    + 'the spread twice.',
  adverse: 'How far the fill landed against us versus the mid at submit, in bps. '
    + 'Positive is worse for us. This is the cost model’s raw material: enough of '
    + 'these and the estimated spread is replaced by a measured one.',
  held: 'Time between opening and closing. Short holds have to overcome the same '
    + 'fixed spread as long ones, which is why scalping is the hardest way to win here.',
  gross: 'Profit before execution cost — what the price move alone gave you.',
  cost: 'What execution took: the spread on both sides, plus any slippage.',
  net: 'What actually reached the account. This is the only number that counts.',
  verdict: 'What THIS trade shows: did the coin go our way, and did the spread leave '
    + 'anything. Nothing more — every active strategy sets its expected edge to zero on '
    + 'purpose, so a single trade is evidence, never a model error. (This column used to '
    + 'read "model wrong" on every trade including the winners, because it compared the '
    + 'result against a prediction that was never made.) Whether a strategy is any good is '
    + 'a question about many trades: the running record is underneath, and the full '
    + 'judgement is on the Daily page.',
  size: 'Position size in dollars, set by fractional Kelly on the LOWER confidence '
    + 'bound of the edge — so uncertainty shrinks the bet rather than inflating it.',
}

/* Local hour of a unix timestamp, as a float so 14:30 sorts after 14:00. */
const hourOf = (ts) => { const d = new Date(ts * 1000); return d.getHours() + d.getMinutes() / 60 }
const parseHM = (s) => { if (!s) return null; const [h, m] = s.split(':').map(Number); return h + (m || 0) / 60 }

export default function Journal() {
  const [tab, setTab] = useState('signals')
  const [strategy, setStrategy] = useState('')
  const [symbol, setSymbol] = useState('')
  const [from, setFrom] = useState(iso(Date.now() - 7 * DAY))
  const [to, setTo] = useState('')
  // Default to trades that were TAKEN. Hundreds of rejected rows saying the same
  // thing is noise; the rejects are still one dropdown away when you want them.
  const [decision, setDecision] = useState('taken')
  const [timeFrom, setTimeFrom] = useState('')
  const [timeTo, setTimeTo] = useState('')
  const [limit, setLimit] = useState(100)

  const f = { strategy: strategy || undefined, symbol: symbol || undefined,
              start_ts: toTs(from), end_ts: toTs(to) }
  const deps = [strategy, symbol, from, to, decision, limit]

  const strats = useAsync(() => api.strategies(), [])
  const signals = useAsync(() => api.signals(limit, { ...f, decision: decision || undefined }), deps, 15000)
  const orders = useAsync(() => api.orders(limit, f), deps, 15000)
  const trades = useAsync(() => api.trades('paper', f), deps, 15000)
  const events = useAsync(() => api.events(limit), [], 15000)

  /* Time-of-day filter is applied here, not in SQL, because it is a filter on the
     CLOCK rather than on the calendar: "every day between 9am and noon" is not a
     range of timestamps. Wrapping past midnight (22:00 to 02:00) is supported. */
  const a = parseHM(timeFrom), b = parseHM(timeTo)
  const inWindow = (ts) => {
    if (a === null && b === null) return true
    const h = hourOf(ts)
    if (a !== null && b !== null) return a <= b ? (h >= a && h <= b) : (h >= a || h <= b)
    if (a !== null) return h >= a
    return h <= b
  }
  const byTime = (rows, key) => (rows || []).filter((r) => inWindow(r[key]))
  const clock = a !== null || b !== null

  return (
    <>
      <h1>Journal</h1>
      <p className="sub">
        The full audit trail, including decisions that did not become trades.<Info text="The rejected signals are usually the more informative half: they show how often the cost hurdle is what stands between a signal and a position." />
      </p>
      <div className="row" style={{ marginBottom: 8 }}>
        {TABS.map((t) => (
          <button key={t} className={tab === t ? 'primary' : ''} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>

      {tab !== 'events' && (
        <div className="filters">
          <label>strategy</label>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="">all</option>
            {(strats.data || []).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
          <label>coin</label>
          <input value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                 placeholder="all" style={{ width: 80 }} />
          {tab === 'signals' && (
            <>
              <label>decision</label>
              <select value={decision} onChange={(e) => setDecision(e.target.value)}>
                <option value="taken">taken only (default)</option>
                <option value="">all, including rejected</option>
                <option value="rejected">rejected only</option>
              </select>
            </>
          )}
          <label>from</label>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          <label>to</label>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          <label>
            time of day
            <Info text="Filters on the clock, across every day in the date range. Use it to ask questions like 'do the morning signals behave differently from the afternoon ones'. Leave both blank for the whole day; a start later than the end wraps past midnight." />
          </label>
          <input type="time" value={timeFrom} onChange={(e) => setTimeFrom(e.target.value)} style={{ width: 108 }} />
          <span className="mut">to</span>
          <input type="time" value={timeTo} onChange={(e) => setTimeTo(e.target.value)} style={{ width: 108 }} />
          <label>
            rows
            <Info text="How many records to fetch. Fewer rows load faster; the filters are applied by the database, so a smaller number does not hide older matches — it shows the most recent ones." />
          </label>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[50, 100, 200, 500, 2000].map((n) => <option key={n} value={n}>{n === 2000 ? 'all' : n}</option>)}
          </select>
          <button className="why" onClick={() => {
            setStrategy(''); setSymbol(''); setDecision('taken'); setTimeFrom(''); setTimeTo(''); setLimit(100)
            setFrom(iso(Date.now() - 7 * DAY)); setTo('')
          }}>clear</button>
        </div>
      )}

      {clock && tab !== 'events' && (
        <div className="mut" style={{ marginBottom: 6 }}>
          clock filter active: {timeFrom || '00:00'}–{timeTo || '23:59'} local
          {a !== null && b !== null && a > b ? ' (wraps past midnight)' : ''}
        </div>
      )}

      {tab === 'signals' && (
        <Card title="signals — every evaluation, taken or not">
          <Table
            cols={[
              { key: 'ts', label: 'time', render: (r) => fmt.time(r.ts) },
              { key: 'strategy', label: 'strategy' },
              { key: 'symbol', label: 'symbol' },
              { key: 'side', label: 'side' },
              { key: 'raw_score', label: 'score', num: true, info: WHAT.score,
                render: (r) => fmt.num(r.raw_score, 2) },
              { key: 'expected_edge_bps', label: 'expected edge', num: true, info: WHAT.edge,
                render: (r) => (
                  <span title={Number.isFinite(r.edge_ci_low_bps)
                    ? `95% interval ${fmt.num(r.edge_ci_low_bps, 0)} to ${fmt.num(r.edge_ci_high_bps, 0)} bps`
                    : 'no interval recorded'}>{fmt.bps(r.expected_edge_bps, 0)}</span>) },
              { key: 'cost_hurdle_bps', label: 'hurdle', num: true, info: WHAT.hurdle,
                render: (r) => (
                  <span title={`${(r.cost_hurdle_bps / 100).toFixed(2)}% — the coin must move at least this much gross`}>
                    {fmt.bps(r.cost_hurdle_bps, 0)}</span>) },
              { key: 'gap', label: 'short by', num: true,
                info: 'How far the signal fell short of the hurdle. Small numbers mean the strategy is close to viable and worth improving; large ones mean it is not in the right business.',
                // This number is computed, not stored on the row, so the table
                // needs an accessor to be able to filter or total it.
                value: (r) => (r.expected_edge_bps ?? 0) - (r.cost_hurdle_bps ?? 0),
                render: (r) => {
                  const g = (r.expected_edge_bps ?? 0) - (r.cost_hurdle_bps ?? 0)
                  return <span className={g >= 0 ? 'pos' : 'mut'}>{g >= 0 ? 'cleared' : fmt.bps(g, 0)}</span>
                } },
              { key: 'decision', label: 'decision', info: WHAT.decision,
                render: (r) => <span className={`pill ${String(r.decision).startsWith('taken') ? 'ok' : ''}`}>{r.decision}</span> },
              { key: 'reject_reason', label: 'why not', info: WHAT.why,
                render: (r) => <span className="mut">{r.reject_reason || '—'}</span> },
            ]}
            rows={byTime(signals.data, 'ts')} />
        </Card>
      )}

      {tab === 'orders' && (
        <Card title="orders">
          <Table
            cols={[
              { key: 'ts_decided', label: 'time', render: (r) => fmt.time(r.ts_decided) },
              { key: 'symbol', label: 'symbol' }, { key: 'side', label: 'side' },
              { key: 'mode', label: 'mode' }, { key: 'status', label: 'status' },
              { key: 'notional_usd', label: 'size', num: true, info: WHAT.size,
                render: (r) => fmt.usd(r.notional_usd) },
              { key: 'mid_at_submit', label: 'mid', num: true,
                info: 'The mid price at the moment the order went out — the reference every cost measurement is taken against.',
                render: (r) => fmt.px(r.mid_at_submit) },
              { key: 'fill_price', label: 'fill', num: true, render: (r) => fmt.px(r.fill_price) },
              { key: 'slip', label: 'adverse', num: true, info: WHAT.adverse,
                render: (r) => (r.fill_price && r.mid_at_submit
                  ? fmt.bps((r.side === 'buy' ? 1 : -1) * (r.fill_price - r.mid_at_submit) / r.mid_at_submit * 1e4, 1) : '—') },
              { key: 'reject_reason', label: 'note', render: (r) => <span className="mut">{r.reject_reason || '—'}</span> },
            ]}
            rows={byTime(orders.data, 'ts_decided')} />
        </Card>
      )}

      {tab === 'trades' && (
        <Card title="closed round trips">
          <Table
            cols={[
              { key: 'ts_close', label: 'closed', render: (r) => fmt.time(r.ts_close) },
              { key: 'symbol', label: 'symbol' }, { key: 'strategy', label: 'strategy' },
              { key: 'holding_s', label: 'held', num: true, info: WHAT.held,
                // Was always minutes, so a 14-hour hold read "840.0m".
                render: (r) => (r.holding_s >= 3600
                  ? `${(r.holding_s / 3600).toFixed(1)}h` : `${(r.holding_s / 60).toFixed(0)}m`) },
              { key: 'notional', label: 'put in', num: true,
                info: 'What the position cost to open: quantity x the fill price. This is the number the percentages are measured against.',
                render: (r) => (r.entry_px && r.qty
                  ? fmt.usd(Math.abs(r.qty * r.entry_px)) : '—') },
              { key: 'proceeds', label: 'got back', num: true,
                info: 'What the sale returned: quantity x the exit fill price. "got back" minus "put in" is the net.',
                render: (r) => (r.exit_px && r.qty
                  ? fmt.usd(Math.abs(r.qty * r.exit_px)) : '—') },
              { key: 'entry_px', label: 'bought at', num: true,
                info: 'The price we actually filled at, spread included — not the mid.',
                render: (r) => <span className="mono">{fmtPx(r.entry_px)}</span> },
              { key: 'exit_px', label: 'sold at', num: true,
                info: 'The price we actually sold at, spread included.',
                render: (r) => <span className="mono">{fmtPx(r.exit_px)}</span> },
              { key: 'move_pct', label: 'move', num: true,
                info: 'Fill to fill. This already has both sides of the spread inside it, which is why a positive move can still be a negative net.',
                render: (r) => {
                  if (!r.entry_px || !r.exit_px) return '—'
                  const m = (r.exit_px / r.entry_px - 1) * 100
                  return <span className={m >= 0 ? 'pos' : 'neg'}>{m >= 0 ? '+' : ''}{m.toFixed(2)}%</span>
                } },
              { key: 'gross_pnl_usd', label: 'coin moved', num: true, info: WHAT.gross,
                render: (r) => fmt.usd(r.gross_pnl_usd) },
              { key: 'cost_usd', label: 'cost', num: true, info: WHAT.cost,
                render: (r) => fmt.usd(r.cost_usd) },
              { key: 'net_pnl_usd', label: 'net', num: true, info: WHAT.net,
                render: (r) => <span className={r.net_pnl_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.net_pnl_usd)}</span> },
              { key: 'attribution_json', label: 'verdict', info: WHAT.verdict,
                render: (r) => {
                  let a = {}
                  try { a = JSON.parse(r.attribution_json) || {} } catch { return '—' }
                  if (!a.verdict) return '—'
                  const good = /^right, and it paid|^paid,/.test(a.verdict)
                  return (
                    <span>
                      <span className={good ? 'pos' : ''}>{a.verdict}</span>
                      {r.excluded_from_learning && (
                        <span className="pill warn" title={r.defect_note || ''}
                              style={{ marginLeft: 6, fontSize: 10 }}>
                          not scored: {(r.defects || []).join(', ')}
                        </span>
                      )}
                      {a.strategy_record && (
                        <span className="mut" style={{ fontSize: 10.5, display: 'block' }}>
                          {a.strategy_record}
                        </span>
                      )}
                    </span>
                  )
                } },
            ]}
            rows={byTime(trades.data, 'ts_close')} />
        </Card>
      )}

      {tab === 'events' && (
        <Card title="system log">
          <Table
            cols={[
              { key: 'ts', label: 'time', render: (r) => fmt.time(r.ts) },
              { key: 'level', label: 'level',
                render: (r) => <span className={`pill ${r.level === 'ERROR' || r.level === 'CRITICAL' ? 'bad' : r.level === 'WARNING' ? 'warn' : ''}`}>{r.level}</span> },
              { key: 'category', label: 'area' },
              { key: 'message', label: 'message' },
            ]}
            rows={events.data || []} />
        </Card>
      )}
    </>
  )
}
