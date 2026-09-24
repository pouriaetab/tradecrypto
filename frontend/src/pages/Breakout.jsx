import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'
import { LineChart } from '../components/charts.jsx'

export default function Breakout({ onModel }) {
  const st = useAsync(() => api.breakoutStatus(), [], 15000)
  const rep = useAsync(() => api.breakoutReport(), [])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [sym, setSym] = useState('BTC')
  const [live, setLive] = useState(null)

  const d = rep.data?.available === false ? null : rep.data
  const o = d?.out_of_sample

  const train = async () => {
    setBusy(true); setErr(null)
    try { await api.breakoutTrain(); rep.reload(); st.reload() }
    catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const calSeries = d?.calibration?.length
    ? [
        { name: 'observed', color: '#3987e5', points: d.calibration.map((c) => ({ x: c.predicted, y: c.observed })) },
        { name: 'perfect calibration', color: '#475569', points: [{ x: 0, y: 0 }, { x: 1, y: 1 }] },
      ]
    : []

  return (
    <>
      <h1>False Breakouts</h1>
      <p className="sub">
        A level broke — is it real, or a trap?<Info text="A level is where resting stop orders sit, which is exactly why breaking it produces guaranteed liquidity for whoever wants to sell into it. Most breaks fail. The mechanical question (did a level break?) is separated from the hard one (is this one real?), and the second is answered with a calibrated probability that has to earn the right to block anything." />
      </p>

      <Card right={<button className="why" onClick={() => onModel('false_breakout')}>full model card</button>}>
        <div className="row">
          <span className={`pill ${st.data?.usable_as_veto ? 'ok' : st.data?.trained ? 'warn' : ''}`}>
            {st.data?.trained ? (st.data.usable_as_veto ? 'veto active' : 'trained, no skill — blocks nothing') : 'not trained'}
          </span>
          {st.data?.n_events != null && <span className="mut">{st.data.n_events} labelled breaks</span>}
          {st.data?.auc != null && <span className="mut mono">AUC {fmt.num(st.data.auc, 3)}</span>}
          <div className="spacer" />
          <button className="primary" onClick={train} disabled={busy}>
            {busy ? 'training…' : 'train / retrain'}
          </button>
        </div>
        {err && <div className="err" style={{ marginTop: 8 }}>{err}</div>}
        {rep.data?.available === false && (
          <div className="mut" style={{ marginTop: 8 }}>{rep.data.note}</div>
        )}
      </Card>

      {d && (
        <>
          <Banner kind={o.beats_chance_at_95 ? 'info' : 'bad'} title={d.verdict}>
            {d.how_to_read}
          </Banner>

          <div className="grid c4">
            <Stat label="Breaks that failed" value={fmt.pct(d.base_rate_false_breakout * 100, 1)}
                  meta="the base rate any model must beat" small />
            <Stat label="Out-of-sample AUC" n={d.n_events} value={fmt.num(o.auc, 3)}
                  meta={`95% CI ${fmt.num(o.auc_ci95[0], 3)}–${fmt.num(o.auc_ci95[1], 3)}`}
                  tone={o.beats_chance_at_95 ? 'pos' : 'neg'} />
            <Stat label="Brier score" value={fmt.num(o.brier, 4)} small
                  meta={`base rate scores ${fmt.num(o.brier_of_base_rate, 4)} — lower is better`} />
            <Stat label="Labelled events" value={d.n_events} small meta={d.direction} />
          </div>

          <h2>What actually predicts failure<Info text="Logistic regression fitted by Newton-Raphson with an L2 penalty, written out rather than imported so every coefficient and standard error is visible here. Positive coefficient = raises the probability the break fails." /></h2>
          <Card>
            <Table
              cols={[
                { key: 'feature', label: 'feature' },
                { key: 'coef', label: 'coefficient', num: true, render: (r) => fmt.num(r.coef, 3) },
                { key: 'z', label: 'z', num: true, render: (r) => fmt.num(r.z, 2) },
                { key: 'odds_ratio', label: 'odds ratio', num: true, render: (r) => fmt.num(r.odds_ratio, 2) },
                { key: 'significant_5pct', label: 'significant',
                  render: (r) => <span className={`pill ${r.significant_5pct ? 'ok' : ''}`}>
                    {r.significant_5pct ? 'yes' : 'no'}</span> },
                { key: 'direction', label: 'effect' },
              ]}
              rows={d.coefficients} />

          </Card>

          <h2>Is the probability trustworthy?<Info text="The blue line should sit on the grey one. If '80% likely to fail' fails 80% of the time the number can be used as a threshold; if it does not, it can only be used as a ranking." /></h2>
          <Card title="reliability — predicted vs observed failure rate">
            <LineChart series={calSeries} height={240}
                       valueFormat={(v) => fmt.pct(v * 100, 0)}
                       xFormat={(x) => fmt.pct(x * 100, 0)} />

            <Table
              cols={[
                { key: 'n', label: 'events', num: true },
                { key: 'predicted', label: 'predicted', num: true, render: (r) => fmt.pct(r.predicted * 100, 1) },
                { key: 'observed', label: 'observed', num: true, render: (r) => fmt.pct(r.observed * 100, 1) },
                { key: 'gap', label: 'gap', num: true,
                  render: (r) => <span className={Math.abs(r.gap) < 0.05 ? 'pos' : 'neg'}>{fmt.pct(r.gap * 100, 1)}</span> },
              ]}
              rows={d.calibration} />
          </Card>

          <h2>Folds</h2>
          <Card>
            <Table
              cols={[
                { key: 'fold', label: 'fold', num: true },
                { key: 'train_events', label: 'train', num: true },
                { key: 'test_events', label: 'test', num: true },
                { key: 'embargo_events', label: 'embargo', num: true },
                { key: 'auc', label: 'AUC', num: true, render: (r) => fmt.num(r.auc, 3) },
                { key: 'brier', label: 'Brier', num: true, render: (r) => fmt.num(r.brier, 4) },
                { key: 'base_rate_false', label: 'base rate', num: true, render: (r) => fmt.pct(r.base_rate_false * 100, 0) },
              ]}
              rows={d.folds} />
          </Card>
        </>
      )}

      <h2>Check a coin right now</h2>
      <Card>
        <div className="filters">
          <input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} style={{ width: 100 }} />
          <button className="primary" onClick={async () => setLive(await api.breakoutVeto(sym))}>check</button>
        </div>
        {live && (
          <>
            <Banner kind={live.block ? 'bad' : 'info'} title={live.block ? 'VETO — do not chase this' : live.state}>
              {live.reason}
            </Banner>
            {live.p_false != null && (
              <>
                <div className="grid c4">
                  <Stat label="P(false breakout)" value={fmt.pct(live.p_false * 100, 0)} small
                        tone={live.block ? 'neg' : 'pos'} />
                  <Stat label="Veto line" value={fmt.pct(live.threshold * 100, 0)} small />
                  <Stat label="Level" value={fmt.num(live.level, 6)} small meta={live.level_kind} />
                  <Stat label="Touches" value={live.level_touches} small />
                </div>
                <Table
                  cols={[
                    { key: 'feature', label: 'driver' },
                    { key: 'value', label: 'value', num: true, render: (r) => fmt.num(r.value, 3) },
                    { key: 'z', label: 'z', num: true, render: (r) => fmt.num(r.z, 2) },
                    { key: 'contribution', label: 'pushes toward', num: true,
                      render: (r) => <span className={r.contribution > 0 ? 'neg' : 'pos'}>
                        {r.contribution > 0 ? 'failure' : 'follow-through'} ({fmt.num(r.contribution, 2)})</span> },
                  ]}
                  rows={live.top_drivers || []} />
              </>
            )}
          </>
        )}
      </Card>
    </>
  )
}
