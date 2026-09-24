import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

function Ticket({ t, onDone }) {
  const [px, setPx] = useState('')
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const k = t.ticket || {}
  const isBuy = t.side === 'buy'
  const secsLeft = Math.max(0, Math.round((k.expires_at || 0) - Date.now() / 1000))

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    try {
      const d = await api.reportFill(t.client_id, { fill_price: Number(px) })
      const c = d.cost_measured || {}
      setMsg(
        `Recorded. Adverse fill: ${fmt.bps(c.half_spread_bps, 1)}. ` +
        (d.position_opened ? `Position open — stop ${fmt.px(d.position_opened.stop)}, target ${fmt.px(d.position_opened.target)}.` : '') +
        (d.trade_closed ? `Round trip net ${fmt.usd(d.trade_closed.net_usd)} over ${d.trade_closed.held_minutes.toFixed(0)}m.` : '')
      )
      onDone()
    } catch (err) { setMsg(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="card" style={{ borderColor: t.intent === 'close' ? '#78350F' : '#1E3A5F' }}>
      <div className="row">
        <span className={`pill ${t.intent === 'close' ? 'warn' : 'ok'}`}>
          {t.intent === 'close' ? 'CLOSE' : 'OPEN'}
        </span>
        <b style={{ fontSize: 16 }}>{isBuy ? 'BUY' : 'SELL'} {t.symbol}</b>
        <span className="mut">{fmt.usd(t.notional_usd)}</span>
        <div className="spacer" />
        <span className={`mut mono ${secsLeft < 60 ? 'neg' : ''}`}>{secsLeft}s left</span>
      </div>

      <div className="grid c3" style={{ marginTop: 10 }}>
        <div><div className="label mut">reference price</div><div className="mono">{fmt.px(t.mid_at_submit)}</div></div>
        <div><div className="label mut">do not pay above</div><div className="mono">{fmt.px(k.max_acceptable_price)}</div></div>
        <div><div className="label mut">strategy</div><div className="mono">{t.strategy}</div></div>
      </div>

      <form onSubmit={submit} className="row">
        <input value={px} onChange={(e) => setPx(e.target.value)} placeholder="actual fill price"
               style={{ width: 170 }} inputMode="decimal" />
        <button className="primary" type="submit" disabled={busy || !px}>report fill</button>
        <button type="button" onClick={() => api.deskSkip(t.client_id).then(onDone)}>didn't take it</button>
      </form>
      {msg && <div className="mono" style={{ marginTop: 8 }}>{msg}</div>}
    </div>
  )
}

function BudgetPanel() {
  const buds = useAsync(() => api.budgets(), [], 8000)
  const [slots, setSlots] = useState(2)
  const [hours, setHours] = useState(24)
  const [err, setErr] = useState(null)

  const create = async () => {
    try { await api.createBudget({ slots: Number(slots), window_hours: Number(hours) }); buds.reload() }
    catch (e) { setErr(e.message) }
  }

  return (
    <Card title="trade budget" right={<Info text="Say how many trades you want over a window and the system holds out for the best ones instead of taking the first that qualify. The bar falls as the deadline approaches but never below the cost hurdle — this is a CAP, not a quota. Asking for 10 and getting 3 means the day only offered 3." />}>
      <div className="filters">
        <label>take</label>
        <input value={slots} onChange={(e) => setSlots(e.target.value)} style={{ width: 60 }} />
        <label>trades over the next</label>
        <select value={hours} onChange={(e) => setHours(e.target.value)}>
          <option value={4}>4 hours</option>
          <option value={8}>8 hours</option>
          <option value={24}>today (to midnight)</option>
          <option value={48}>2 days</option>
          <option value={72}>3 days</option>
        </select>
        <button className="primary" onClick={create}>set budget</button>
      </div>
      {err && <div className="err">{err}</div>}
      {(buds.data || []).map((b) => (
        <div key={b.id} className="card" style={{ marginTop: 10, marginBottom: 0 }}>
          <div className="row">
            <b>#{b.id}</b>
            <span className="pill ok">{b.slots_left} of {b.slots_total} slots left</span>
            <span className="mut">{b.time_left_hours.toFixed(1)}h remaining</span>
            <div className="spacer" />
            <button onClick={() => api.cancelBudget(b.id).then(buds.reload)}>cancel</button>
          </div>
          <div className="progress" style={{ marginTop: 10 }}>
            <div className="progress-fill" style={{ width: `${b.pct_window_elapsed}%` }} />
          </div>
          <div className="grid c3" style={{ marginTop: 10 }}>
            <Stat label="Current bar to clear" small
                  value={b.current_threshold_bps != null ? fmt.bps(b.current_threshold_bps, 0) : '—'}
                  meta={b.threshold_status} />
            <Stat label="Typical signal" small
                  value={b.quality_median_bps != null ? fmt.bps(b.quality_median_bps, 0) : '—'}
                  meta={`${b.quality_samples} samples`} />
            <Stat label="Good signal (p90)" small
                  value={b.quality_p90_bps != null ? fmt.bps(b.quality_p90_bps, 0) : '—'} />
          </div>

        </div>
      ))}
      {(buds.data || []).length === 0 && (
        <div className="mut">No budget set — the normal risk limits apply on their own.</div>
      )}
    </Card>
  )
}

export default function TradeDesk() {
  const desk = useAsync(() => api.desk('advisory'), [], 5000)
  const safety = useAsync(() => api.safety(), [])
  const d = desk.data

  return (
    <>
      <ExperimentSwitch />
      <h1>Trade Desk</h1>
      <p className="sub">
        The engine decides, you execute in Robinhood, you report the fill.<Info text="Nothing here places an order on your behalf. Reporting the price you actually got is the measurement that teaches the cost model what this venue really charges — it is not paperwork." />
      </p>

      {safety.data?.execution_mode !== 'advisory' && (
        <Banner kind="warn" title={`Engine is in ${safety.data?.execution_mode || '…'} mode, not advisory`}>
          Set <code>TC_EXECUTION_MODE=advisory</code> in <code>.env</code> and restart for tickets to
          appear here. In paper mode the engine fills its own orders and this page stays empty.
        </Banner>
      )}


      <BudgetPanel />

      <h2>Open tickets</h2>
      {(d?.open_tickets || []).length === 0
        ? <Card><div className="mut">No live tickets. The engine posts one when a signal clears the cost hurdle
            and passes every risk check.</div></Card>
        : (d.open_tickets || []).map((t) => (
            <Ticket key={t.client_id} t={t} onDone={desk.reload} />
          ))}

      <h2>Positions you are holding</h2>
      <Card>
        <Table
          cols={[
            { key: 'symbol', label: 'symbol' },
            { key: 'strategy', label: 'strategy' },
            { key: 'qty', label: 'qty', num: true, render: (r) => fmt.num(r.qty, 4) },
            { key: 'avg_px', label: 'entry', num: true, render: (r) => fmt.px(r.avg_px) },
            { key: 'stop_px', label: 'stop', num: true, render: (r) => fmt.px(r.stop_px) },
            { key: 'target_px', label: 'target', num: true, render: (r) => fmt.px(r.target_px) },
            { key: 'held_minutes', label: 'held', num: true, render: (r) => `${(r.held_minutes || 0).toFixed(0)}m` },
            { key: 'deadline_minutes', label: 'time left', num: true,
              render: (r) => <span className={(r.deadline_minutes ?? 1) < 0 ? 'neg' : ''}>{(r.deadline_minutes || 0).toFixed(0)}m</span> },
            { key: 'close_ticket_pending', label: 'exit',
              render: (r) => r.close_ticket_pending
                ? <span className="pill warn">exit ticket posted</span>
                : <span className="mut">holding</span> },
          ]}
          rows={d?.positions || []} empty="flat" />
      </Card>

      <h2>Recent fills you reported</h2>
      <Card>
        <Table
          cols={[
            { key: 'ts_filled', label: 'time', render: (r) => fmt.time(r.ts_filled) },
            { key: 'symbol', label: 'symbol' }, { key: 'side', label: 'side' },
            { key: 'intent', label: 'intent' },
            { key: 'mid_at_submit', label: 'reference', num: true, render: (r) => fmt.px(r.mid_at_submit) },
            { key: 'fill_price', label: 'you got', num: true, render: (r) => fmt.px(r.fill_price) },
            { key: 'slip', label: 'adverse', num: true,
              render: (r) => (r.fill_price && r.mid_at_submit
                ? <span className="neg">{fmt.bps((r.side === 'buy' ? 1 : -1) * (r.fill_price - r.mid_at_submit) / r.mid_at_submit * 1e4, 1)}</span>
                : '—') },
          ]}
          rows={d?.recent_fills || []}
          empty="none yet — each one you report makes the cost model less of a guess" />
      </Card>
    </>
  )
}

function ExperimentSwitch() {
  const st = useAsync(() => api.experiment(), [])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const d = st.data
  if (!d) return null
  const flip = async () => {
    setBusy(true)
    try {
      const r = await api.setExperiment(!d.enabled)
      setMsg(r.enabled
        ? 'On. Restart on the Setup page, then it will start taking paper trades.'
        : 'Off. Restart to apply.')
      st.reload()
    } catch (e) { setMsg(String(e)) }
    setBusy(false)
  }
  return (
    <Banner kind={d.enabled ? 'warn' : 'info'}
            title={d.enabled ? 'paper experiment is ON' : 'paper experiment is off'}>
      {d.what}
      <div style={{ marginTop: 6 }}>
        <button className={d.enabled ? '' : 'primary'} disabled={busy} onClick={flip}>
          {busy ? 'saving…' : d.enabled ? 'turn it off' : 'take the trades anyway (paper only)'}
        </button>
        <span className="mut" style={{ marginLeft: 8 }}>
          mode: {d.mode} · real money enabled: {String(d.live_enabled)} {msg}
        </span>
      </div>
    </Banner>
  )
}
