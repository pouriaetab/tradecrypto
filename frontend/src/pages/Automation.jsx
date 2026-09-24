import React from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

const dur = (s) => {
  if (s == null) return '—'
  if (s < 90) return `${Math.round(s)}s`
  if (s < 5400) return `${Math.round(s / 60)}m`
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
}

export default function Automation() {
  const st = useAsync(() => api.scheduler(), [], 8000)
  const d = st.data
  // "run now" fired and then sat there looking identical, so there was no way to
  // tell a click that worked from one that did nothing. Each row now says what
  // it is doing and what came back.
  const [busy, setBusy] = React.useState(null)
  const [said, setSaid] = React.useState(null)
  // Jobs run one at a time on the backend, so clicking a second one while the
  // first is going used to do nothing useful. Queue them instead: click as many
  // as you want and they start in order, each when the one before it finishes.
  const [queue, setQueue] = React.useState([])
  const [done, setDone] = React.useState([])

  const runOne = React.useCallback(async (name) => {
    setBusy(name)
    let line
    try {
      const r = await api.schedulerRun(name)
      const ok = !r || r.status !== 'error'
      const secs = r && r.duration_s ? ` (${Math.round(r.duration_s)}s)` : ''
      line = { name, ok, text: ((r && (r.summary || r.error)) || 'finished') + secs }
    } catch (e) {
      // A timeout does not stop the job -- it is still running on the backend
      // and its row fills in on the next poll.
      const slow = /no answer in|taking longer/i.test(e.message || '')
      line = { name, ok: slow, text: slow
        ? 'still running on the backend — this row updates by itself when it finishes'
        : e.message }
    }
    setSaid(line)
    setDone((d) => [...d, line])
    setBusy(null)
    st.reload()
  }, [st])

  // Drain the queue: whenever nothing is running and something is waiting, go.
  React.useEffect(() => {
    if (busy || queue.length === 0) return
    const [next, ...rest] = queue
    setQueue(rest)
    runOne(next)
  }, [busy, queue, runOne])

  const enqueue = (name) => {
    setSaid(null)
    if (!busy && queue.length === 0) { runOne(name); return }
    setQueue((q) => (q.includes(name) || busy === name ? q : [...q, name]))
  }

  return (
    <>
      <h1>Automation</h1>
      <p className="sub">
        Training, backtesting and validation run on their own.
        <Info text="Jobs run one at a time so a heavy walk-forward never competes with live data collection. A job that fails backs off rather than retrying in a loop. Everything here is state in the database, so a restart resumes where it left off." />
      </p>

      {busy && <Banner kind="warn" title={`Running ${busy}…`}>
        Jobs run one at a time. Heavy ones (model_lab ~7m, breakout_train ~33m) take a while —
        this page keeps refreshing and the result lands in the job's row.
        {queue.length > 0 && <> Queued next: <strong>{queue.join(' → ')}</strong>.</>}
      </Banner>}
      {!busy && queue.length > 0 && <Banner kind="warn" title="Queued">
        {queue.join(' → ')}
      </Banner>}
      {done.length > 0 && <Card>
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <strong>This session</strong>
          <button className="why" onClick={() => setDone([])}>clear</button>
        </div>
        {done.map((l, i) => (
          <div key={i} className="step-detail" style={{ marginTop: 4 }}>
            <span className={l.ok ? 'pos' : 'neg'}>{l.ok ? '✓' : '✗'}</span>{' '}
            <strong>{l.name}</strong> — {l.text}
          </div>
        ))}
      </Card>}
      {said && !busy && queue.length === 0 &&
        <Banner kind={said.ok ? 'ok' : 'bad'} title={`${said.name} ${said.ok ? 'finished' : 'failed'}`}>
          {said.text}
        </Banner>}

      {d?.paused && <Banner kind="warn" title="Paused">Nothing will run until you resume.</Banner>}
      {!d?.running && <Banner kind="bad" title="Scheduler stopped">Research is not updating itself.</Banner>}

      <Card>
        <div className="row">
          <span className={`pill ${d?.running && !d?.paused ? 'ok' : 'bad'}`}>
            {!d?.running ? 'stopped' : d?.paused ? 'paused' : 'running'}
          </span>
          {d?.current_job && <span className="mut mono">now: {d.current_job}</span>}
          <div className="spacer" />
          {d?.running
            ? <>
                <button onClick={() => api.schedulerPause(!d.paused).then(st.reload)}>
                  {d.paused ? 'resume' : 'pause'}
                </button>
                <button className="danger" onClick={() => api.schedulerStop().then(st.reload)}>stop</button>
              </>
            : <button className="primary" onClick={() => api.schedulerStart().then(st.reload)}>start</button>}
        </div>
      </Card>

      <h2>Jobs</h2>
      <Card>
        <Table
          cols={[
            { key: 'name', label: 'job',
              render: (r) => <span>{r.name}{r.what && <Info text={r.what} />}</span> },
            { key: 'last_status', label: 'last',
              render: (r) => <span className={`pill ${r.last_status === 'ok' ? 'ok' : r.last_status === 'error' ? 'bad' : ''}`}>
                {r.last_status || 'never run'}</span> },
            { key: 'last_run', label: 'when', render: (r) => (r.last_run ? fmt.time(r.last_run) : '—') },
            { key: 'due_in_s', label: 'next', num: true, render: (r) => dur(r.due_in_s) },
            { key: 'interval_s', label: 'every', num: true, render: (r) => dur(r.interval_s) },
            { key: 'last_duration_s', label: 'took', num: true, render: (r) => dur(r.last_duration_s) },
            { key: 'run_count', label: 'runs', num: true },
            { key: 'last_summary', label: 'result',
              render: (r) => <span className={r.last_status === 'error' ? 'neg' : 'mut'}>
                {r.last_error || r.last_summary || '—'}</span> },
            { key: 'controls', label: 'actions',
              render: (r) => (
                <span className="row" style={{ gap: 4 }}>
                  <button className="primary"
                          title="Run now. If something else is already running, this waits its turn — click as many as you like."
                          onClick={() => enqueue(r.name)}>
                    {busy === r.name ? 'running…'
                      : queue.includes(r.name) ? `queued #${queue.indexOf(r.name) + 1}`
                      : 'run now'}</button>
                  <button className="why" onClick={() => api.schedulerEnable(r.name, !r.enabled).then(st.reload)}>
                    {r.enabled ? 'disable' : 'enable'}
                  </button>
                </span>) },
          ]}
          rows={d?.jobs || []} />
      </Card>

      <div className="grid c4">
        <Stat label="Jobs" value={(d?.jobs || []).length} small />
        <Stat label="Enabled" value={(d?.jobs || []).filter((j) => j.enabled).length} small />
        <Stat label="Failing" small tone={(d?.jobs || []).some((j) => j.last_status === 'error') ? 'neg' : 'pos'}
              value={(d?.jobs || []).filter((j) => j.last_status === 'error').length} />
        <Stat label="Stale" small value={(d?.jobs || []).filter((j) => j.stale).length}
              meta="overdue by 3× their interval" />
      </div>
    </>
  )
}
