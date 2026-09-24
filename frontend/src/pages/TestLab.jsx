import React, { useEffect, useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Card, Info, Table, Banner, useAsync } from '../components/ui.jsx'

/* Three layers, deliberately separate. The middle one is the point: a component
   cannot be the witness for its own correctness, so the independent checks
   re-derive the same claims by a different route. */
const LAYERS = {
  suite: 'The project’s own pytest files — what the authors assert is true.',
  independent: 'Re-derives the same claims by a DIFFERENT method, sharing no code '
    + 'with what it checks. If auc() had a bug, every test that called auc() would '
    + 'agree with it. These would not.',
  user: 'Anything you write in tests/user/. Same runner, same reporting, never '
    + 'overwritten by an update.',
}

function Result({ r }) {
  if (!r) return null
  return (
    <div className={`hint-more ${r.ok ? '' : 'neg'}`} style={{ marginTop: 6 }}>
      <b>{r.ok ? 'passed' : 'FAILED'}</b>
      {r.passed !== undefined && <> — {r.passed} passed, {r.failed} failed
        {r.errors ? `, ${r.errors} errors` : ''}{r.skipped ? `, ${r.skipped} skipped` : ''}
        {r.duration_s !== undefined && ` in ${r.duration_s.toFixed(1)}s`}</>}
      {r.output && <pre className="scroll" style={{ maxHeight: 380, marginTop: 6 }}>{r.output}</pre>}
    </div>
  )
}

export default function TestLab() {
  const cat = useAsync(() => api.tests(), [])
  const hist = useAsync(() => api.testsHistory(25), [], 20000)
  const [busy, setBusy] = useState('')
  const [runs, setRuns] = useState({})          // target -> result
  const [verify, setVerify] = useState(null)
  const [open, setOpen] = useState({})
  const [file, setFile] = useState('test_my_checks.py')
  const [src, setSrc] = useState('')
  const [saved, setSaved] = useState('')

  useEffect(() => { api.testsUserRead('').then((d) => setSrc(d.template || '')).catch(() => {}) }, [])

  const go = async (target, label) => {
    setBusy(label); setSaved('')
    // await has to happen HERE, not inside the setRuns updater: that updater is
    // a plain arrow function, and `await` inside a non-async function is a
    // syntax error that takes the whole app down at build time.
    try {
      const r = await api.testsRun(target)
      setRuns((p) => ({ ...p, [label]: r }))
    } catch (e) {
      setRuns((p) => ({ ...p, [label]: { ok: false, output: String(e) } }))
    }
    setBusy(''); hist.reload?.()
  }
  const goVerify = async (key) => {
    setBusy(key || 'verify-all')
    try {
      const r = await api.testsVerify(key)
      setVerify(r)
    } catch (e) { setVerify({ checks: [], error: String(e) }) }
    setBusy(''); hist.reload?.()
  }
  const save = async () => {
    setSaved('')
    try {
      const r = await api.testsUserSave(file, src)
      setSaved(r.error ? `could not save — ${r.error}` : `saved ${r.saved}`)
      if (!r.error) cat.reload?.()
    } catch (e) { setSaved(String(e)) }
  }

  const c = cat.data
  const features = c?.features || []

  return (
    <>
      <h1>Test Lab</h1>
      <p className="sub">
        Run every check yourself, and add your own.
        <Info text="Four bugs in this project were dangerous because they were silent: a memory watchdog that never started, a false-breakout model trained on 8% of its data, a supervisor that read a crash as a clean shutdown, and a cost hurdle below the real spread. None raised. None logged. A green dashboard is a claim; this page is the evidence." />
      </p>

      <div className="row" style={{ marginBottom: 10 }}>
        <button className="primary" disabled={!!busy} onClick={() => go('', 'everything')}>
          {busy === 'everything' ? 'running…' : 'run every test'}
        </button>
        <button disabled={!!busy} onClick={() => goVerify('')}>
          {busy === 'verify-all' ? 'checking…' : 'run the independent cross-check'}
        </button>
        <span className="mut">{c ? `${c.total} tests in the suite` : 'loading…'}</span>
      </div>
      <Result r={runs['everything']} />

      {/* ── independent ───────────────────────────────────────────────────── */}
      <Card title="independent cross-check">
        <div className="hint">{LAYERS.independent}</div>
        {(c?.independent?.checks || []).map((k) => {
          const res = (verify?.checks || []).find((x) => x.key === k.key)
          return (
            <div key={k.key} className="rowblock">
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <div>
                  <b>{k.title}</b>
                  {res && <span className={`pill ${res.ok ? 'ok' : 'bad'}`} style={{ marginLeft: 8 }}>
                    {res.ok ? 'pass' : 'FAIL'}</span>}
                  <div className="mut" style={{ fontSize: 12 }}>
                    audits <code>{k.audits}</code>
                    <Info text={k.method} label="method" />
                  </div>
                </div>
                <button className="why" disabled={!!busy} onClick={() => goVerify(k.key)}>
                  {busy === k.key ? '…' : 'run'}
                </button>
              </div>
              {res && (
                <div className={`hint-more ${res.ok ? '' : 'neg'}`}>
                  {res.detail}
                  {res.threshold ? <div className="mut">pass condition: {res.threshold}</div> : null}
                  {res.traceback && <pre className="scroll" style={{ maxHeight: 200 }}>{res.traceback}</pre>}
                </div>
              )}
            </div>
          )
        })}
        {verify?.error && <Banner kind="bad" title="could not run">{verify.error}</Banner>}
      </Card>

      {/* ── per feature ───────────────────────────────────────────────────── */}
      <Card title="tests by feature">
        <div className="hint">{LAYERS.suite}</div>
        {features.map((f) => (
          <div key={f.key} className="rowblock">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div>
                <button className="linkish" onClick={() => setOpen((o) => ({ ...o, [f.key]: !o[f.key] }))}>
                  {open[f.key] ? '▾' : '▸'} <b>{f.title}</b>
                </button>
                <span className="mut"> · {f.n_tests} tests</span>
                <div className="mut" style={{ fontSize: 12 }}>
                  {f.what}{f.code ? <> <code>{f.code}</code></> : null}
                </div>
              </div>
              <div className="row">
                {f.files.filter((x) => !x.missing).map((x) => (
                  <button key={x.file} className="why" disabled={!!busy}
                          onClick={() => go(x.file, x.file)}>
                    {busy === x.file ? '…' : `run ${x.file}`}
                  </button>
                ))}
              </div>
            </div>
            {f.files.map((x) => runs[x.file] && <Result key={`r${x.file}`} r={runs[x.file]} />)}
            {open[f.key] && f.files.map((x) => (
              <div key={x.file} style={{ marginTop: 6 }}>
                <div className="mut">{x.file}{x.missing ? ' — MISSING' : ''}</div>
                <Table
                  cols={[
                    { key: 'name', label: 'test' },
                    { key: 'doc', label: 'what it asserts',
                      render: (r) => <span className="mut">{r.doc || '—'}</span> },
                    { key: 'run', label: '',
                      render: (r) => (
                        <button className="why" disabled={!!busy}
                                onClick={() => go(r.node_id, r.node_id)}>
                          {busy === r.node_id ? '…' : 'run'}</button>) },
                  ]}
                  rows={x.tests} empty="no tests in this file" />
                {x.tests.map((t) => runs[t.node_id] && <Result key={t.node_id} r={runs[t.node_id]} />)}
              </div>
            ))}
          </div>
        ))}
      </Card>

      {/* ── your own ──────────────────────────────────────────────────────── */}
      <Card title="your tests">
        <div className="hint">{LAYERS.user}</div>
        <div className="row" style={{ marginBottom: 6 }}>
          <label>file</label>
          <input value={file} onChange={(e) => setFile(e.target.value)} style={{ width: 220 }} />
          <button onClick={save}>save</button>
          <button className="why" disabled={!!busy} onClick={() => go(`user/${file}`, `user/${file}`)}>
            {busy === `user/${file}` ? '…' : 'save then run'}
          </button>
          <span className={saved.startsWith('could not') ? 'neg' : 'mut'}>{saved}</span>
        </div>
        <textarea value={src} onChange={(e) => setSrc(e.target.value)} spellCheck={false}
                  style={{ width: '100%', minHeight: 300, fontFamily: 'ui-monospace, monospace',
                           fontSize: 12, lineHeight: 1.45 }} />
        <Result r={runs[`user/${file}`]} />
        {(c?.your_tests?.files || []).length > 0 && (
          <div className="mut" style={{ marginTop: 6 }}>
            on disk: {c.your_tests.files.map((x) => x.file).join(', ')}
          </div>
        )}
      </Card>

      <Card title="run history">
        <Table
          cols={[
            { key: 'ts', label: 'when', render: (r) => fmt.time(r.ts) },
            { key: 'target', label: 'target' },
            { key: 'kind', label: 'layer',
              info: 'suite = the project’s own tests. independent = the cross-check that shares no code with what it audits.' },
            { key: 'passed', label: 'passed', num: true },
            { key: 'failed', label: 'failed', num: true,
              render: (r) => <span className={r.failed ? 'neg' : 'mut'}>{r.failed}</span> },
            { key: 'duration_s', label: 'took', num: true,
              render: (r) => `${(r.duration_s || 0).toFixed(1)}s` },
            { key: 'exit_code', label: 'result',
              render: (r) => <span className={`pill ${r.exit_code === 0 ? 'ok' : 'bad'}`}>
                {r.exit_code === 0 ? 'green' : 'red'}</span> },
          ]}
          rows={hist.data || []} empty="no runs yet" />
      </Card>
    </>
  )
}
