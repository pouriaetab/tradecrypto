import React, { useState } from 'react'
import { api } from '../lib/api.js'
import { Card, Info, Banner, useAsync } from '../components/ui.jsx'

/* Everything here used to be a list of terminal commands. It did not have to be:
   the backend runs on this machine, as you, so it can install its own
   dependencies, make its own keys and restart itself. The only step left for a
   person is the one a person must do — sign in to Robinhood. */

function PhoneCard() {
  const acc = useAsync(() => api.access(), [], 0)
  const [msg, setMsg] = useState('')
  const a = acc.data
  if (!a) return null
  const toggle = (on) => api.setPhoneAccess(on)
    .then((r) => { setMsg(`${r.next} ${r.note || ''}`); acc.reload() })
    .catch((e) => setMsg(String(e.message || e)))
  return (
    <Card title="on your phone">
      {a.enabled ? (
        <>
          <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
            <span className="pill ok">{a.how}</span>
            {a.works_away_from_home && <span className="pill">works on cell data</span>}
          </div>
          {a.url ? (
            <div className="row" style={{ flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
              <code style={{ fontSize: 12, overflowWrap: 'anywhere' }}>{a.url}</code>
              <button onClick={() => navigator.clipboard?.writeText(a.url)}>copy link</button>
            </div>
          ) : (
            <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>{a.how}</div>
          )}
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>
            {a.install}{a.note ? ' ' + a.note : ''}
          </div>
          {a.token && (
            <div className="row" style={{ gap: 8, marginTop: 6 }}>
              <span className="mut" style={{ fontSize: 11 }}>token</span>
              <code style={{ fontSize: 11 }}>{a.token}</code>
              <button onClick={() => navigator.clipboard?.writeText(a.token)}>copy</button>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span>Phone access is off.</span>
            <button className="primary" onClick={() => toggle(true)}>turn it on</button>
          </div>
          <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>{a.how_to_enable}</div>
          <div className="mut" style={{ fontSize: 11, marginTop: 4 }}>{a.why_off_by_default}</div>
        </>
      )}
      {a.enabled && (
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => toggle(false)}>turn phone access off</button>
        </div>
      )}
      {msg && <div className="mut" style={{ fontSize: 11, marginTop: 6 }}>{msg}</div>}
    </Card>
  )
}

export default function Setup() {
  const st = useAsync(() => api.setupSteps(), [], 8000)
  const rec = useAsync(() => api.setupRecoveredDb(), [])
  const [busy, setBusy] = useState('')
  const [out, setOut] = useState(null)
  const [pub, setPub] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [msg, setMsg] = useState('')

  const run = async (key, fn) => {
    setBusy(key); setMsg(''); setOut(null)
    try {
      const r = await fn()
      setOut(r)
      if (r?.public_key) setPub(r.public_key)
      if (r?.error) setMsg(r.error)
      st.reload()
    } catch (e) { setMsg(String(e)) }
    setBusy('')
  }

  const d = st.data
  const act = {
    install: () => run('install', () => api.setupInstall('pynacl')),
    generate: () => run('generate', () => api.setupKeypair()),
    sync: () => run('sync', () => api.rhSyncUniverse()),
    spreads: () => run('spreads', () => api.rhMeasureSpreads()),
  }

  return (
    <>
      <h1>Setup</h1>
      <p className="sub">
        Connect Robinhood. Five steps, four of them buttons.
        <Info text="The backend runs on your machine as you, so it can install what it needs, generate its own keys and restart itself. The only step that needs a person is signing in to Robinhood — that is your account." />
      </p>

      {d && (
        <Banner kind={d.complete ? 'info' : 'warn'}
                title={d.complete ? 'connected' : `${d.done} of ${d.total} done`}>
          {d.complete
            ? <>Robinhood is connected{d.account?.account_number ? ` — account ${d.account.account_number}` : ''}. Nothing else is needed.</>
            : <>Work down the list. Anything greyed out is waiting on the step above it.</>}
        </Banner>
      )}

      {rec.data?.available && (
        <Banner kind="warn" title="a repaired database is ready to install">
          {rec.data.bars?.toLocaleString()} bars, integrity check {rec.data.integrity}.
          Installing swaps it in and restarts; the damaged file is kept, not deleted.
          <div style={{ marginTop: 6 }}>
            <button className="primary" disabled={busy === 'restoredb'}
                    onClick={() => run('restoredb', () => api.setupRestoreDb())}>
              {busy === 'restoredb' ? 'installing…' : 'install the repaired database'}
            </button>
          </div>
        </Banner>
      )}

      <div className="row" style={{ marginBottom: 10 }}>
        <button disabled={busy === 'restart'}
                onClick={() => run('restart', () => api.setupRestart())}>
          {busy === 'restart' ? 'restarting…' : 'restart the backend'}
        </button>
        <span className="mut">
          needed after anything that changes code or settings — it comes back on its own
        </span>
      </div>

      {/* Reaching the app from a phone. Off by default, and the card says why
          rather than just offering a switch. */}
      <PhoneCard />

      <Card title="steps">
        {(d?.steps || []).map((s) => (
          <div key={s.key} className="rowblock" style={{ opacity: s.blocked ? 0.45 : 1 }}>
            <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div style={{ maxWidth: '66%' }}>
                <span className={`pill ${s.done ? 'ok' : s.blocked ? '' : 'warn'}`}>
                  {s.done ? 'done' : s.blocked ? 'waiting' : 'to do'}
                </span>
                <b style={{ marginLeft: 8 }}>{s.n}. {s.label}</b>
                <div className="mut" style={{ fontSize: 12 }}>{s.detail}</div>
                <div className="mut" style={{ fontSize: 11, marginTop: 2 }}>{s.why}</div>
              </div>
              <div>
                {s.button && !s.done && !s.blocked && (
                  <button className="primary" disabled={!!busy}
                          onClick={act[s.button]}>
                    {busy === s.button ? 'working…' : s.button === 'install' ? 'install it'
                      : s.button === 'generate' ? 'make my keys'
                      : s.button === 'sync' ? 'ask Robinhood' : 'measure now'}
                  </button>
                )}
                {s.manual && !s.done && <span className="mut">your move ↓</span>}
              </div>
            </div>
          </div>
        ))}
      </Card>

      {out?.restart_required && (
        <Banner kind="warn" title="restart needed">
          Installed. The app has to restart before it can use it.
          <div style={{ marginTop: 6 }}>
            <button className="primary" disabled={busy === 'restart'}
                    onClick={() => run('restart', () => api.setupRestart())}>
              {busy === 'restart' ? 'restarting…' : 'restart now'}
            </button>
            <span className="mut" style={{ marginLeft: 8 }}>
              comes back on its own in a few seconds
            </span>
          </div>
        </Banner>
      )}

      {pub && (
        <Card title="step 3 — the only part I cannot do for you">
          <div className="hint">
            This is your Robinhood account, so you have to be the one signed in.
          </div>
          <ol style={{ marginTop: 4 }}>
            <li>Copy the public key below.</li>
            <li>Open Robinhood <b>web classic</b> → crypto account settings → <b>Add key</b>.</li>
            <li>Paste it, and enable read-only permissions to start with.</li>
            <li>Robinhood shows you an API key. Paste that back here.</li>
          </ol>
          <textarea readOnly value={pub} onFocus={(e) => e.target.select()}
                    style={{ width: '100%', height: 54, fontFamily: 'ui-monospace, monospace', fontSize: 12 }} />
          <div className="mut" style={{ fontSize: 11 }}>
            The private half was written to {d?.credentials_file} with 0600 permissions.
            It is not shown here on purpose, and it never leaves this machine.
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <input value={apiKey} onChange={(e) => setApiKey(e.target.value)}
                   placeholder="rh-api-…" style={{ width: 340 }} />
            <button className="primary" disabled={!apiKey || busy === 'key'}
                    onClick={() => run('key', () => api.setupApiKey(apiKey))}>
              {busy === 'key' ? 'checking…' : 'save and connect'}
            </button>
          </div>
        </Card>
      )}

      {(msg || out) && (
        <Card title="result">
          {msg && <div className="neg">{msg}</div>}
          {out && <pre className="scroll" style={{ maxHeight: 260, fontSize: 11 }}>
            {JSON.stringify(out, null, 2)}</pre>}
        </Card>
      )}
    </>
  )
}
