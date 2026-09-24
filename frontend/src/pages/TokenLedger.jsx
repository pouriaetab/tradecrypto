import React, { useState } from 'react'
import { api } from '../lib/api.js'
import { Card, Info, Banner, useAsync } from '../components/ui.jsx'

export default function TokenLedger() {
  const led = useAsync(() => api.tokens(), [])
  const [msg, setMsg] = useState('')
  const [open, setOpen] = useState({})
  const d = led.data

  const pick = async (feature, provider) => {
    setMsg('')
    try {
      const r = await api.tokensSetProvider(feature, provider)
      setMsg(r.error || `${feature} → ${provider}`)
      if (!r.error) led.reload()
    } catch (e) { setMsg(String(e)) }
  }

  return (
    <>
      <h1>Token Ledger</h1>
      <p className="sub">
        Which features spend model tokens, and on what.
        <Info text="Declared per feature, then checked against the source every time this page loads. A ledger that is hand-maintained is a ledger that is wrong." />
      </p>

      {d && (
        <Banner kind={d.spending_now?.length ? 'warn' : 'info'} title={d.headline}>
          {d.audit_note}
          <div className="mut" style={{ marginTop: 4 }}>
            execution mode: <b>{d.mode}</b>
            {d.mode === 'paper' && ' — in paper mode nothing reaches a broker, so the one feature that needs a model is never called.'}
          </div>
        </Banner>
      )}

      {d?.discrepancies?.length > 0 && (
        <Banner kind="bad" title="the source disagrees with the declaration">
          {d.discrepancies.join(', ')} — a model SDK or endpoint appears in a feature
          that claims to use none. Trust this box, not the row.
        </Banner>
      )}

      <Card title="features">
        {(d?.features || []).map((f) => (
          <div key={f.key} className="rowblock">
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div style={{ maxWidth: '62%' }}>
                <b>{f.title}</b>
                <span className={`pill ${f.needs_model ? 'warn' : 'ok'}`} style={{ marginLeft: 8 }}>
                  {f.needs_model ? 'needs a model' : f.could_use_model ? 'model optional' : 'never a model'}
                </span>
                {f.spending_now && <span className="pill bad" style={{ marginLeft: 6 }}>spending now</span>}
                <div className="mut" style={{ fontSize: 12, marginTop: 2 }}>{f.what}</div>
                <button className="linkish" onClick={() => setOpen((o) => ({ ...o, [f.key]: !o[f.key] }))}>
                  {open[f.key] ? 'less' : 'why'}
                </button>
              </div>
              <div style={{ textAlign: 'right' }}>
                {f.switchable ? (
                  <select value={f.provider} onChange={(e) => pick(f.key, e.target.value)}>
                    {(d.providers || [])
                      .filter((p) => !(p.key === 'none' && f.needs_model))
                      .map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
                  </select>
                ) : (
                  <span className="mut">no model, by design</span>
                )}
                <div className="mut" style={{ fontSize: 11, marginTop: 2 }}>
                  currently: {f.status_when_off}
                </div>
              </div>
            </div>
            {open[f.key] && (
              <div className="hint-more">
                {f.why}
                <div style={{ marginTop: 6 }}>
                  <b>source check</b> — {f.audit.files_scanned} files:{' '}
                  <span className={f.audit.discrepancy ? 'neg' : 'pos'}>{f.audit.verdict}</span>
                  <div className="mut" style={{ fontSize: 11 }}>{f.audit.method}</div>
                  {f.audit.sdk_imports?.length > 0 && (
                    <div className="neg">SDK imports: {f.audit.sdk_imports.join('; ')}</div>)}
                  {f.audit.model_hosts?.length > 0 && (
                    <div className="neg">model endpoints: {f.audit.model_hosts.join(', ')}</div>)}
                  {f.audit.other_hosts?.length > 0 && (
                    <div className="mut">contacts: {f.audit.other_hosts.join(', ')}</div>)}
                  <div className="mut">code: <code>{f.modules.join(', ')}</code></div>
                </div>
              </div>
            )}
          </div>
        ))}
        {msg && <div className={msg.includes('cannot') || msg.includes('deliberately') ? 'neg' : 'mut'}
                     style={{ marginTop: 8 }}>{msg}</div>}
      </Card>

      <Card title="what each provider costs you">
        {(d?.providers || []).map((p) => (
          <div key={p.key} className="rowblock">
            <b>{p.label}</b> <span className="mut">· {p.cost}</span>
            {p.note && <div className="mut" style={{ fontSize: 12 }}>{p.note}</div>}
          </div>
        ))}
        <div className="hint" style={{ marginTop: 8 }}>
          Order placement is the only feature that cannot be made free, because Robinhood
          has no REST endpoint for crypto orders — an MCP agent has to call the tools.
          But the model there is only formatting a decision numpy already made, so the
          cheapest capable model is the right one. The agent is a typist, not a trader.
        </div>
      </Card>
    </>
  )
}
