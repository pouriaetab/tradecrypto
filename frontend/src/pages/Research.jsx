import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table } from '../components/ui.jsx'

export default function Research({ onModel }) {
  const [busy, setBusy] = useState(null)
  const [bt, setBt] = useState(null)
  const [cs, setCs] = useState(null)
  const [wf, setWf] = useState(null)
  const [err, setErr] = useState(null)
  const [strategy, setStrategy] = useState('top_mover_reversal')

  const go = async (kind, fn, setter) => {
    setBusy(kind); setErr(null)
    try { setter(await fn()) } catch (e) { setErr(e.message) } finally { setBusy(null) }
  }

  const s = bt?.summary
  return (
    <>
      <h1>Research</h1>
      <p className="sub">
        Cost sensitivity first, walk-forward last — only the last one's verdict counts.<Info text="Cost sensitivity comes first because it can rule out a strategy before you bother validating it. A single backtest is not evidence: parameters were fitted near that data. The walk-forward is the only test whose verdict counts." />
      </p>

      <Card>
        <div className="row">
          <label className="mut">strategy</label>
          <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="top_mover_reversal">top_mover_reversal</option>
            <option value="xs_momentum">xs_momentum</option>
          </select>
          <div className="spacer" />
          <button disabled={busy} onClick={() => go('cs', () => api.costSensitivity({ strategy }), setCs)}>
            {busy === 'cs' ? 'running…' : '1. cost sensitivity'}
          </button>
          <button disabled={busy} onClick={() => go('bt', () => api.backtest({ strategy }), setBt)}>
            {busy === 'bt' ? 'running…' : '2. backtest'}
          </button>
          <button disabled={busy} className="primary" onClick={() => go('wf', () => api.walkforward({ strategy }), setWf)}>
            {busy === 'wf' ? 'running…' : '3. walk-forward'}
          </button>
        </div>
        {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
      </Card>

      {cs?.insufficient_data && (
        <Banner kind="warn" title="Not enough history yet">
          Only {cs.bars_available} bars stored. Public feeds serve a few hundred recent candles at a time,
          and the engine saves every one it fetches — leave it running and this fills in.
        </Banner>
      )}

      {cs?.curve && (
        <>
          <h2>1. Does the edge survive execution cost?</h2>
          <Banner kind="info">{cs.how_to_read} Your measured cost is{' '}
            <b>{fmt.bps(cs.measured_cost_bps_per_side, 0)} per side ({cs.measured_source})</b>.</Banner>
          <Card>
            <Table
              cols={[
                { key: 'cost_bps_per_side', label: 'cost/side', num: true, render: (r) => fmt.bps(r.cost_bps_per_side, 0) },
                { key: 'round_trip_pct', label: 'round trip', num: true, render: (r) => fmt.pct(r.round_trip_pct) },
                { key: 'n_trades', label: 'trades', num: true },
                { key: 'mean_net_bps', label: 'mean net', num: true,
                  render: (r) => <span className={r.mean_net_bps > 0 ? 'pos' : 'neg'}>{fmt.bps(r.mean_net_bps, 1)}</span> },
                { key: 'hit_rate', label: 'hit rate', num: true, render: (r) => fmt.pct((r.hit_rate || 0) * 100, 1) },
                { key: 'profitable_at_95pct_confidence', label: 'profitable @95%',
                  render: (r) => <span className={`pill ${r.profitable_at_95pct_confidence ? 'ok' : 'bad'}`}>
                    {r.profitable_at_95pct_confidence ? 'yes' : 'no'}</span> },
              ]}
              rows={cs.curve}
            />
          </Card>
        </>
      )}

      {s && (
        <>
          <h2>2. In-sample backtest</h2>
          <Banner kind="warn" title="This number is not evidence on its own">
            A single backtest on data the parameters were fitted near will almost always look positive.
            It is here to catch obvious breakage, not to justify trading. Only the walk-forward verdict counts.
          </Banner>
          <div className="grid c4">
            <Stat label="Trades" value={s.n} small />
            <Stat label="Mean net / trade" n={s.n} value={fmt.bps(s.mean_return_bps, 1)}
                  tone={s.mean_return_bps > 0 ? 'pos' : 'neg'}
                  meta={`CI [${fmt.bps((s.mean_return_ci?.[0] ?? 0) * 1e4, 0)}, ${fmt.bps((s.mean_return_ci?.[1] ?? 0) * 1e4, 0)}]`} />
            <Stat label="Hit rate" n={s.n} value={fmt.pct(s.hit_rate * 100, 1)} />
            <Stat label="Deflated Sharpe" n={s.n} value={fmt.num(s.deflated_sharpe, 3)}
                  meta={`${s.deflated_sharpe_detail?.n_trials} configurations counted`}
                  model="deflated_sharpe" onModel={onModel}
                  tone={s.deflated_sharpe > 0.95 ? 'pos' : 'neg'} />
            <Stat label="Max drawdown" n={s.n} value={fmt.pct(s.max_drawdown?.max_drawdown_pct)} />
            <Stat label="Signals dropped (no shorting)" value={s.dropped_short_signals} small
                  meta="Robinhood does not allow shorting crypto" />
            <Stat label="Rejected below cost hurdle" value={s.rejected_below_hurdle} small />
            <Stat label="Min track record needed" value={fmt.num(s.min_track_record_length, 0)} small
                  meta="observations before the Sharpe means anything" />
          </div>
          {s.assumption_tests && (
            <Card title="assumption tests">
              <Table
                cols={[
                  { key: 'test', label: 'test' },
                  { key: 'p', label: 'p-value', num: true },
                  { key: 'verdict', label: 'verdict' },
                  { key: 'why', label: 'why it matters' },
                ]}
                rows={Object.entries(s.assumption_tests)
                  .filter(([, v]) => v && typeof v === 'object' && 'p_value' in v)
                  .map(([k, v]) => ({
                    test: k.replace(/_/g, ' '),
                    p: fmt.num(v.p_value, 4),
                    verdict: Object.entries(v).find(([kk]) => kk.endsWith('_at_5pct'))?.[1] ? 'ok' : 'violated',
                    why: v.why_it_matters,
                  }))}
              />
            </Card>
          )}
        </>
      )}

      {wf && !wf.insufficient_data && (
        <>
          <h2>3. Walk-forward — the verdict that counts</h2>
          <Banner kind={wf.verdict === 'EDGE_SURVIVES_TESTING' ? 'info' : 'bad'} title={wf.verdict.replace(/_/g, ' ')}>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {wf.verdict_reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>
          </Banner>
          <div className="grid c4">
            <Stat label="Configurations tested" value={wf.n_configurations_tested} small
                  meta="all of them counted against the Sharpe" />
            <Stat label="Folds" value={wf.n_folds} small meta={`${wf.embargo_bars}-bar embargo`} />
            <Stat label="Best OOS mean" n={wf.best_out_of_sample?.n}
                  value={wf.best_out_of_sample ? fmt.bps(wf.best_out_of_sample.mean_return_bps, 1) : '—'}
                  tone={(wf.best_out_of_sample?.mean_return_bps ?? 0) > 0 ? 'pos' : 'neg'} />
            <Stat label="PBO" value={wf.pbo?.pbo != null ? fmt.num(wf.pbo.pbo, 2) : '—'}
                  meta={wf.pbo?.reliable ? 'reliable' : wf.pbo?.note} model="pbo" onModel={onModel} />
          </div>
          <Card title="per configuration (out of sample)">
            <Table
              cols={[
                { key: 'params', label: 'params', render: (r) => <span className="mono">{JSON.stringify(r.params)}</span> },
                { key: 'n_oos_trades', label: 'trades', num: true },
                { key: 'mean_bps', label: 'mean net', num: true,
                  render: (r) => <span className={(r.mean_bps ?? 0) > 0 ? 'pos' : 'neg'}>{fmt.bps(r.mean_bps, 1)}</span> },
                { key: 'sharpe_per_trade', label: 'Sharpe/trade', num: true, render: (r) => fmt.num(r.sharpe_per_trade, 3) },
              ]}
              rows={wf.per_configuration || []}
            />
          </Card>
        </>
      )}
    </>
  )
}
