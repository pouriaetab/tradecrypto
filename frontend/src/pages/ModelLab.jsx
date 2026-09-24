import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

const OPERATOR = ['fast_flip', 'forced_momentum', 'regime_swing', 'lead_lag_rotation']
const OTHER = ['top_mover_reversal', 'xs_momentum']

function SplitBar({ split, bars }) {
  const seg = (a, b, cls, label) => {
    const w = Math.max(0, ((b - a) / Math.max(bars, 1)) * 100)
    return <div key={label} className={`splitseg ${cls}`} style={{ width: `${w}%` }} title={`${label}: bars ${a}–${b}`}>{w > 8 ? label : ''}</div>
  }
  return (
    <>
      <div className="splitbar">
        {seg(split.train[0], split.train[1], 'train', 'train')}
        {seg(split.train[1], split.validation[0], 'emb', 'embargo')}
        {seg(split.validation[0], split.validation[1], 'val', 'validation')}
        {seg(split.validation[1], split.test[0], 'emb', 'embargo')}
        {seg(split.test[0], split.test[1], 'test', 'test')}
      </div>
      <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>{split.note}</div>
    </>
  )
}

function perfRow(label, p) {
  return {
    pass: label,
    n: p?.n ?? 0,
    mean: p?.n ? fmt.bps(p.mean_return_bps, 1) : '—',
    hit: p?.n ? fmt.pct(p.hit_rate * 100, 1) : '—',
    sharpe: p?.n ? fmt.num(p.sharpe_per_trade, 3) : '—',
    dd: p?.n ? fmt.pct(p.max_drawdown?.max_drawdown_pct) : '—',
    exits: p?.exit_reasons ? Object.entries(p.exit_reasons).map(([k, v]) => `${k} ${v}`).join(', ') : '—',
    _pos: (p?.mean_return_bps ?? 0) > 0,
  }
}

export default function ModelLab({ onModel }) {
  const [strategy, setStrategy] = useState('regime_swing')
  const [wf, setWf] = useState(false)
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const strats = useAsync(() => api.operatorStrategies(), [])

  const run = async () => {
    setBusy(true); setErr(null)
    try { setReport(await api.modelLab(strategy, wf)) }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const d = report
  const meta = (strats.data || []).find((s) => s.name === strategy)

  return (
    <>
      <h1>Model Lab</h1>
      <p className="sub">
        One strategy end to end: split, calibration, gates, verdict.<Info text="This is the designated place. If a claim about a strategy is not on this page, it is not a claim the system is making. Thresholds are set before results are seen." />
      </p>

      <Card>
        <div className="row">
          <label className="mut">strategy</label>
          <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setReport(null) }}>
            <optgroup label="your four ideas">
              {OPERATOR.map((s) => <option key={s} value={s}>{s}</option>)}
            </optgroup>
            <optgroup label="reference hypotheses">
              {OTHER.map((s) => <option key={s} value={s}>{s}</option>)}
            </optgroup>
          </select>
          <label className="mut" style={{ marginLeft: 12 }}>
            <input type="checkbox" checked={wf} onChange={(e) => setWf(e.target.checked)} style={{ marginRight: 6 }} />
            include walk-forward (slow)
          </label>
          <div className="spacer" />
          {meta && <span className="mut mono">{meta.n_configurations} configurations in its grid</span>}
          <button className="primary" onClick={run} disabled={busy}>{busy ? 'running…' : 'run full lifecycle'}</button>
          {meta && <button className="why" onClick={() => onModel(meta.model_card)}>model card</button>}
        </div>
        {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
      </Card>

      {d && d.verdict === 'INSUFFICIENT_DATA' && (
        <Banner kind="warn" title="Not enough history yet">
          {d.data.bars} bars stored, {d.data.required_bars} needed for this strategy
          (it needs {d.data.warmup_bars} bars just to warm up). {d.next_action}
        </Banner>
      )}

      {d && d.verdict !== 'INSUFFICIENT_DATA' && (
        <>
          <Banner kind={d.verdict === 'APPROVED_FOR_PAPER' ? 'info' : 'bad'} title={d.verdict.replace(/_/g, ' ')}>
            {d.next_action}
          </Banner>

          <h2>1. Data</h2>
          <div className="grid c4">
            <Stat label="Bars" value={d.data.bars} small meta={`${d.data.symbols} symbols`} />
            <Stat label="Span" value={`${d.data.span_days.toFixed(1)} d`} small meta="7 days minimum" />
            <Stat label="Warmup needed" value={`${d.data.warmup_bars} bars`} small />
            <Stat label="Estimated round-trip cost" value={fmt.pct(d.cost.round_trip_pct)} small
                  meta={d.cost.source} model="symbol_cost_estimator" onModel={onModel} />
          </div>

          <h2>2. Split</h2>
          <Card><SplitBar split={d.split} bars={d.data.bars} /></Card>

          <h2>3. Calibration (training data only)</h2>
          <Card>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className={`pill ${d.calibration.claims_edge ? 'ok' : 'bad'}`}>
                {d.calibration.claims_edge ? 'claims an edge' : 'claims no edge'}
              </span>
              <span className="mut">{String(d.calibration.coefficients.calib_note || '')}</span>
            </div>
            <Table
              cols={[{ key: 'k', label: 'coefficient' }, { key: 'v', label: 'value' }]}
              rows={Object.entries(d.calibration.coefficients).map(([k, v]) => ({
                k, v: typeof v === 'object' ? JSON.stringify(v) : String(v),
              }))} />
            <div className="mut" style={{ marginTop: 8, fontSize: 12 }}>{d.calibration.rule}</div>
          </Card>

          <h2>4. Acceptance gates</h2>
          <Card>
            <Table
              cols={[
                { key: 'passed', label: '', render: (g) => <span className={`pill ${g.passed ? 'ok' : 'bad'}`}>{g.passed ? 'pass' : 'fail'}</span> },
                { key: 'name', label: 'gate' },
                { key: 'question', label: 'question' },
                { key: 'threshold', label: 'required' },
                { key: 'actual', label: 'actual' },
                { key: 'blocking', label: 'blocking', render: (g) => (g.blocking ? 'yes' : 'advisory') },
              ]}
              rows={d.gates} />
            <button className="why" onClick={() => onModel('acceptance_gates')}>why these gates</button>
          </Card>

          <h2>5. Performance by block<Info text="Train performance is not evidence — the coefficients were fitted there. Validation is where you are allowed to iterate. Test is looked at once. If you change parameters after reading the test row, the test row is no longer a test." /></h2>
          <Card>
            <Table
              cols={[
                { key: 'pass', label: 'block' }, { key: 'n', label: 'trades', num: true },
                { key: 'mean', label: 'mean net', num: true,
                  render: (r) => <span className={r._pos ? 'pos' : 'neg'}>{r.mean}</span> },
                { key: 'hit', label: 'hit rate', num: true },
                { key: 'sharpe', label: 'Sharpe/trade', num: true },
                { key: 'dd', label: 'max DD', num: true },
                { key: 'exits', label: 'exit mix' },
              ]}
              rows={['train', 'validation', 'test'].map((k) => perfRow(k, d.performance[k]))} />
            
          </Card>

          {d.diagnostic_no_hurdle && (
            <>
              <h2>6. Diagnostic — hurdle disabled<Info text="These trades were NOT allowed to happen: the calibrated edge did not clear the hurdle, so the engine declined them. This block shows what the signal was pointing at and how it would have performed — a diagnostic, never a performance claim." /></h2>
              <Card>
                <Table
                  cols={[
                    { key: 'pass', label: 'block' }, { key: 'n', label: 'trades', num: true },
                    { key: 'mean', label: 'mean net', num: true,
                      render: (r) => <span className={r._pos ? 'pos' : 'neg'}>{r.mean}</span> },
                    { key: 'hit', label: 'hit rate', num: true },
                    { key: 'exits', label: 'exit mix' },
                  ]}
                  rows={['validation', 'test'].map((k) => perfRow(k, d.diagnostic_no_hurdle[k]))} />

              </Card>
            </>
          )}

          <h2>7. Cost sensitivity — the decisive test<Info text="Read the row nearest the estimated cost. Where mean net turns negative is the break-even cost. If that is below Robinhood's actual spread, the strategy cannot work here regardless of the signal." /></h2>
          <Card>
            <Table
              cols={[
                { key: 'cost_bps_per_side', label: 'cost/side', num: true, render: (r) => fmt.bps(r.cost_bps_per_side, 0) },
                { key: 'round_trip_pct', label: 'round trip', num: true, render: (r) => fmt.pct(r.round_trip_pct) },
                { key: 'n_trades', label: 'trades', num: true },
                { key: 'mean_net_bps', label: 'mean net', num: true,
                  render: (r) => <span className={(r.mean_net_bps ?? 0) > 0 ? 'pos' : 'neg'}>{fmt.bps(r.mean_net_bps, 1)}</span> },
                { key: 'hit_rate', label: 'hit', num: true, render: (r) => fmt.pct((r.hit_rate || 0) * 100, 1) },
                { key: 'profitable_at_95pct_confidence', label: 'profitable @95%',
                  render: (r) => <span className={`pill ${r.profitable_at_95pct_confidence ? 'ok' : 'bad'}`}>
                    {r.profitable_at_95pct_confidence ? 'yes' : 'no'}</span> },
              ]}
              rows={d.cost_sensitivity || []} />

          </Card>

          {d.walk_forward && (
            <>
              <h2>8. Walk-forward</h2>
              <Banner kind={d.walk_forward.verdict === 'EDGE_SURVIVES_TESTING' ? 'info' : 'bad'}
                      title={d.walk_forward.verdict.replace(/_/g, ' ')}>
                <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                  {d.walk_forward.verdict_reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </Banner>
              <Card>
                <Table
                  cols={[
                    { key: 'params', label: 'params', render: (r) => <span className="mono">{JSON.stringify(r.params)}</span> },
                    { key: 'n_oos_trades', label: 'OOS trades', num: true },
                    { key: 'mean_bps', label: 'mean net', num: true,
                      render: (r) => <span className={(r.mean_bps ?? 0) > 0 ? 'pos' : 'neg'}>{fmt.bps(r.mean_bps, 1)}</span> },
                    { key: 'sharpe_per_trade', label: 'Sharpe', num: true, render: (r) => fmt.num(r.sharpe_per_trade, 3) },
                  ]}
                  rows={d.walk_forward.per_configuration || []} />
              </Card>
            </>
          )}

          {d.calendar_effects?.available && (
            <>
              <h2>9. Calendar effects — is "early morning" real?</h2>
              <Banner kind="info" title={d.calendar_effects.verdict}>{d.calendar_effects.warning}</Banner>
              <Card>
                <Table
                  cols={[
                    { key: 'bucket', label: 'hour' },
                    { key: 'n', label: 'obs', num: true },
                    { key: 'mean_bps', label: 'mean fwd', num: true, render: (r) => fmt.bps(r.mean_bps, 1) },
                    { key: 'p_value', label: 'p', num: true, render: (r) => fmt.num(r.p_value, 3) },
                    { key: 'survives_fdr', label: 'survives correction',
                      render: (r) => <span className={`pill ${r.survives_fdr ? 'ok' : ''}`}>{r.survives_fdr ? 'yes' : 'no'}</span> },
                  ]}
                  rows={(d.calendar_effects.hour_of_day || []).filter((r) => r.sufficient)} />
              </Card>
            </>
          )}

          {d.regime_now?.available && (
            <>
              <h2>Market regime right now</h2>
              <div className="grid c3">
                <Stat label="Breadth" small model="regime_detector" onModel={onModel}
                      value={d.regime_now.breadth.value != null ? fmt.pct(d.regime_now.breadth.value * 100) : '—'}
                      meta={d.regime_now.breadth.interpretation} />
                <Stat label="Leader trend" small
                      value={d.regime_now.leader_trend.interpretation || '—'}
                      meta={`slope ${fmt.bps(d.regime_now.leader_trend.ma_slope_bps, 0)}`} />
                <Stat label="Risk on?" small value={d.regime_now.risk_on ? 'yes' : 'no'}
                      tone={d.regime_now.risk_on ? 'pos' : 'neg'} meta={d.regime_now.dispersion.interpretation} />
              </div>
            </>
          )}
        </>
      )}
    </>
  )
}
