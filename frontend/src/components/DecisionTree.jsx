/**
 * The live decision, opened all the way up.
 *
 * Strategy -> the field it looked at -> each coin -> the variables behind it,
 * the gates it had to pass, the odds on its target -> the one that was funded.
 * Every level collapses, because the whole thing at once is unreadable.
 *
 * Nothing here is computed in the browser. Every number comes from
 * /api/v1/decision-tree, which reads what the engine already recorded and
 * refuses to invent the parts that were never recorded.
 */
import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, useAsync } from './ui.jsx'

const ROLE_LABEL = {
  ranks: 'ranks',
  gate: 'gate',
  recorded: 'recorded',
}
const ROLE_HELP = {
  ranks: 'This variable enters the score that decides which coin wins.',
  gate: 'This variable can refuse a coin outright. It does not change the ranking.',
  recorded: 'Stored with the decision for later study. It changes nothing today.',
}

function Caret({ open }) {
  return <span className="dt-caret" aria-hidden>{open ? '▾' : '▸'}</span>
}

function Node({ title, meta, tone, children, defaultOpen = false, count }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`dt-node ${tone || ''}`}>
      <button className="dt-head" onClick={() => setOpen(!open)}>
        <Caret open={open} />
        <span className="dt-title">{title}</span>
        {count != null && <span className="dt-count">{count}</span>}
        <span className="dt-meta mut">{meta}</span>
      </button>
      {open && <div className="dt-body">{children}</div>}
    </div>
  )
}

function num(v, d = 4) {
  if (v === null || v === undefined) return '—'
  if (typeof v !== 'number') return String(v)
  if (!isFinite(v)) return '—'
  const a = Math.abs(v)
  return a >= 1000 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : v.toFixed(d)
}

function Variables({ rows }) {
  if (!rows.length) return <div className="mut">nothing recorded</div>
  return (
    <table className="dt-vars">
      <thead>
        <tr>
          <th>part it plays</th><th>variable</th><th className="num">value</th>
          <th className="num">coef</th><th className="num">z</th>
          <th className="num">odds ratio</th><th>evidence</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((v) => {
          const im = v.importance
          return (
            <tr key={v.name} className={`dt-role-${v.role}`}>
              <td>
                <span className={`pill dt-pill-${v.role}`} title={ROLE_HELP[v.role]}>
                  {ROLE_LABEL[v.role] || v.role}
                </span>
              </td>
              <td className="mono">{v.name}</td>
              <td className="num mono">{num(v.value)}</td>
              <td className="num mono">{im ? num(im.coef, 3) : '—'}</td>
              <td className="num mono">{im ? num(im.z, 2) : '—'}</td>
              <td className="num mono">{im ? num(im.odds_ratio, 2) : '—'}</td>
              <td className="mut">
                {im
                  ? (im.significant
                      ? <span style={{ color: 'var(--pos)' }}>carries information</span>
                      : <span>inside the noise</span>)
                  : '—'}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Candidate({ c, expr }) {
  const tone = c.promoted ? 'ok' : 'off'
  const meta = [
    `#${c.rank}`,
    `score ${num(c.raw_score, 3)}`,
    c.gates_total ? `${c.gates_passed}/${c.gates_total} gates` : null,
    c.odds ? `${Math.round(c.odds.p_reach * 100)}% to target` : null,
    c.promoted ? 'FUNDED' : (c.reject_reason || 'not taken'),
  ].filter(Boolean).join(' · ')

  return (
    <Node title={<span className="mono">{c.symbol}</span>} meta={meta} tone={tone}>
      <div className="dt-section-label">
        variables <Info text={
          'Only variables marked "ranks" decide which coin wins — for these ' +
          'strategies that is a one-line expression, not a fitted model. ' +
          'coef / z / odds ratio come from entry_quality, fitted offline on ' +
          'this strategy’s own past signals; they are evidence about the ' +
          'variables and change no trade.'} />
      </div>
      <div className="mono mut dt-expr">{expr || 'score expression not recorded'}</div>
      <Variables rows={c.variables || []} />

      {c.odds && (
        <>
          <div className="dt-section-label">odds on the target</div>
          <div className="dt-odds">
            <b>{Math.round(c.odds.p_reach * 100)}%</b> chance of reaching the
            target · half get there by <b>{c.odds.p50_h}h</b>, seven in ten by{' '}
            <b>{c.odds.p70_h}h</b>
            <div className="mut" style={{ marginTop: 4 }}>{c.odds.source}</div>
          </div>
        </>
      )}

      {!!(c.gates || []).length && (
        <>
          <div className="dt-section-label">
            gates it had to pass ({c.gates_passed}/{c.gates_total})
          </div>
          <table className="dt-vars">
            <thead><tr><th>gate</th><th>result</th><th className="num">limit</th>
              <th className="num">reading</th><th>what it means</th></tr></thead>
            <tbody>
              {c.gates.map((g, i) => (
                <tr key={i}>
                  <td className="mono">{g.check}</td>
                  <td><span className={`pill ${g.passed ? 'ok' : 'bad'}`}>
                    {g.passed ? 'pass' : 'blocked'}</span></td>
                  <td className="num mono">{num(g.limit)}</td>
                  <td className="num mono">{num(g.actual)}</td>
                  <td className="mut">{g.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {!(c.gates || []).length && (
        <div className="mut" style={{ marginTop: 8 }}>
          No risk-gate record for this one — the gate chain is written when a
          signal reaches the risk check, and this one did not get that far.
        </div>
      )}
    </Node>
  )
}

function ModelLayer({ m }) {
  const hp = m.hour_profile
  const q = m.quality
  const v = m.versions || {}
  return (
    <>
      <div className="dt-section-label">
        the one weight that is fitted, not written
      </div>
      {hp ? (
        <div className="dt-odds">
          <b>hour_weight</b> — {hp.is_flat
            ? <span>flat at 1.00 right now</span>
            : <span>varies by hour</span>}
          {hp.n != null && <span className="mut"> · from {hp.n} samples</span>}
          <div className="mut" style={{ marginTop: 4 }}>{hp.what}</div>
          {hp.verdict && <div className="mut">{hp.verdict}</div>}
        </div>
      ) : <div className="mut">no hour profile stored yet</div>}

      <div className="dt-section-label">
        models tried for this strategy
        <Info text="Champion vs challenger over PARAMETERS, not coefficients. A challenger is promoted only if it beats the champion by two standard errors on held-out trades." />
      </div>
      {v.note && <div className="mut" style={{ marginBottom: 6 }}>{v.note}</div>}
      {(v.rivals || []).length ? (
        <table className="dt-vars">
          <thead><tr><th>fitted</th><th>standing</th><th className="num">live trades</th><th>verdict</th></tr></thead>
          <tbody>
            {v.rivals.map((r, i) => (
              <tr key={i}>
                <td className="mut">{fmt.time ? fmt.time(r.fitted_ts) : r.fitted_ts}</td>
                <td>{r.is_champion
                  ? <span className="pill ok">champion</span>
                  : <span className="pill">{r.promoted ? 'was champion' : 'rejected'}</span>}</td>
                <td className="num mono">{r.n_live_trades ?? '—'}</td>
                <td className="mut">{r.verdict}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <div className="mut">no retraining runs recorded yet</div>}

      <div className="dt-section-label">
        which variables actually carried information
        <Info text="Fitted offline on this strategy's own past signals. Advisory only — it does not pick trades." />
      </div>
      {q ? (
        <>
          <div className="dt-odds">
            AUC <b>{num(q.auc, 3)}</b>
            {q.auc_ci95 && q.auc_ci95[0] != null &&
              <span className="mut"> (95% CI {num(q.auc_ci95[0], 3)} – {num(q.auc_ci95[1], 3)})</span>}
            {' · '}
            {q.beats_chance
              ? <span style={{ color: 'var(--pos)' }}>beats a coin flip</span>
              : <span className="mut">cannot be told apart from a coin flip</span>}
            <span className="mut"> · {q.n_events} past signals</span>
            <div className="mut" style={{ marginTop: 4 }}>{q.note}</div>
          </div>
          <Variables rows={(q.coefficients || []).map((c) => ({
            name: c.feature, value: null, role: 'recorded', importance: {
              coef: c.coef, z: c.z, std_error: c.std_error,
              odds_ratio: c.odds_ratio, significant: c.significant_5pct,
            },
          }))} />
        </>
      ) : (
        <div className="mut">
          Not fitted yet. This needs a few hundred of this strategy's own past
          signals before it can say anything — until then there is no honest
          per-variable number to show, so none is shown.
        </div>
      )}
    </>
  )
}

export default function DecisionTree() {
  const tree = useAsync(() => api.decisionTree(), [], 20000)
  const [showNotes, setShowNotes] = useState(false)
  const d = tree.data

  if (tree.err) return <Banner kind="bad" title="could not load the decision tree">{String(tree.err)}</Banner>
  if (!d) return <div className="mut">loading…</div>

  return (
    <Card
      title="What it is deciding right now"
      right={<button className="why" onClick={() => setShowNotes(!showNotes)}>
        {showNotes ? 'hide' : 'what is real here'}
      </button>}
    >
      {showNotes && (
        <Banner kind="info" title="What these numbers are, and are not">
          <ul className="dt-notes">
            {(d.honesty || []).map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </Banner>
      )}

      <div className="dt-tree">
        {(d.strategies || []).map((s) => {
          const c = s.considered
          const meta = [
            `${c.universe} coins looked at`,
            `${c.emitted} setups found`,
            s.selected ? `funded ${s.selected.symbol}` : 'nothing funded',
            s.ts ? fmt.time(s.ts) : 'no decision yet',
          ].join(' · ')
          return (
            <Node
              key={s.strategy}
              title={<span className="mono">{s.strategy}</span>}
              meta={meta}
              count={c.emitted}
              tone={s.selected ? 'ok' : 'off'}
              defaultOpen={!!s.selected}
            >
              <div className="mono mut dt-expr">
                {(s.recipe || {}).expr || 'scoring expression not recorded'}
              </div>
              {(s.recipe || {}).plain && (
                <div className="mut dt-plain">{s.recipe.plain}</div>
              )}

              <Node title="the field" meta={c.note} count={c.emitted} defaultOpen>
                {(s.candidates || []).length
                  ? s.candidates.map((cd) => (
                      <Candidate key={cd.symbol} c={cd} expr={(s.recipe || {}).expr} />
                    ))
                  : <div className="mut">
                      No setup passed this strategy's own entry conditions on the
                      last bar. That is a decision too — it is what "no trade"
                      looks like from the inside.
                    </div>}
              </Node>

              <Node title="the model behind it" meta="weights, rivals, evidence">
                <ModelLayer m={s.model || {}} />
              </Node>
            </Node>
          )
        })}
      </div>
    </Card>
  )
}
