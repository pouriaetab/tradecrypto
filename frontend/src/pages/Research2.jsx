import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

const SEV = { critical: 'bad', serious: 'bad', warning: 'warn', info: '' }

export default function NewsAndPapers({ onModel }) {
  const [tab, setTab] = useState('news')
  const [sym, setSym] = useState('')
  const news = useAsync(() => api.news(60, sym || undefined), [sym], 120000)
  const alerts = useAsync(() => api.newsAlerts(24), [], 120000)
  const [status, setStatus] = useState('')
  const [tag, setTag] = useState('')
  const lib = useAsync(() => api.library(status || undefined, tag || undefined), [status, tag])
  const [busy, setBusy] = useState(false)

  return (
    <>
      <h1>News &amp; Research</h1>
      <div className="row" style={{ marginBottom: 12 }}>
        <div className="seg">
          <button className={tab === 'news' ? 'on' : ''} onClick={() => setTab('news')}>market &amp; news</button>
          <button className={tab === 'papers' ? 'on' : ''} onClick={() => setTab('papers')}>papers read</button>
        </div>
      </div>

      {tab === 'news' && (
        <>
          <p className="sub">
            News is context, not signal — used here defensively.
            <Info text="By the time a headline reaches a feed it has been in the market for seconds to minutes and the fast money has traded it. The one actionable use is defence: a coin being delisted, drained or sued is not a coin whose statistics apply." />
          </p>

          {alerts.data?.flagged_symbols?.length > 0 && (
            <Banner kind="bad" title={`Do not trade: ${alerts.data.flagged_symbols.join(', ')}`}>
              {alerts.data.recommendation}
            </Banner>
          )}

          <div className="filters">
            <label>coin</label>
            <input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())}
                   placeholder="all" style={{ width: 90 }} />
            <div className="spacer" />
            <button disabled={busy} onClick={async () => {
              setBusy(true)
              try { await api.newsFetch(); news.reload(); alerts.reload() } finally { setBusy(false) }
            }}>{busy ? 'fetching…' : 'refresh feeds'}</button>
          </div>

          <Card right={<button className="why" onClick={() => onModel('news_events')}>model card</button>}>
            <Table
              cols={[
                { key: 'ts', label: 'when', render: (r) => fmt.time(r.ts) },
                { key: 'severity', label: '', render: (r) => <span className={`pill ${SEV[r.severity] || ''}`}>{r.severity}</span> },
                { key: 'title', label: 'headline',
                  render: (r) => <a href={r.link} target="_blank" rel="noreferrer">{r.title}</a> },
                { key: 'symbols', label: 'coins', render: (r) => (r.symbols || []).join(', ') || '—' },
                { key: 'categories', label: 'type', render: (r) => (r.categories || []).join(', ') || '—' },
                { key: 'source', label: 'source' },
              ]}
              rows={news.data?.rows || []}
              empty="no headlines stored yet — hit refresh feeds" />

          </Card>
        </>
      )}

      {tab === 'papers' && (
        <>
          <p className="sub">
            Everything behind this system, and what each piece is used for.
            <Info text="'Applied' means a specific line of code implements it and the entry names the file — not a claim of familiarity. 'Read' and 'queued' are a reading list with reasons." />
          </p>

          {lib.data && (
            <div className="grid c4">
              <Stat label="Papers" value={lib.data.counts.total} small />
              <Stat label="Applied in code" value={lib.data.counts.applied} small tone="pos" />
              <Stat label="Read" value={lib.data.counts.read} small />
              <Stat label="Queued" value={lib.data.counts.queued} small />
            </div>
          )}

          <div className="filters">
            <button onClick={async () => {
              setBusy(true)
              try { await api.libraryFetch(); lib.reload() } finally { setBusy(false) }
            }} disabled={busy}>{busy ? 'checking arXiv…' : 'check for new papers'}</button>
            <Info text="Pulls recent submissions from arXiv q-fin (trading and microstructure, statistical finance, computational finance, portfolio management), keeps the ones whose abstract mentions things this system actually does, and files them as queued. Runs weekly on its own from the Automation page." />
            <label>status</label>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all</option>
              <option value="applied">applied</option>
              <option value="read">read</option>
              <option value="queued">queued</option>
            </select>
            <label>tag</label>
            <select value={tag} onChange={(e) => setTag(e.target.value)}>
              <option value="">all</option>
              {Object.keys(lib.data?.tags || {}).map((t) => (
                <option key={t} value={t}>{t} ({lib.data.tags[t]})</option>
              ))}
            </select>
          </div>

          {(lib.data?.papers || []).map((p) => (
            <div key={p.key} className="card">
              <div className="row">
                <span className={`pill ${p.status === 'applied' ? 'ok' : p.status === 'read' ? 'warn' : ''}`}>
                  {p.status}
                </span>
                <b><a href={p.url} target="_blank" rel="noreferrer">{p.title}</a></b>
                <div className="spacer" />
                <span className="mut mono" style={{ fontSize: 11 }}>{p.year}</span>
              </div>
              <div className="mut" style={{ fontSize: 12, margin: '4px 0' }}>
                {p.authors} · {p.venue}
              </div>
              <div style={{ marginBottom: 6 }}>{p.one_line}</div>
              <div className="mut" style={{ fontSize: 12 }}><b>Used for:</b> {p.used_for}</div>
              <div className="row" style={{ marginTop: 6 }}>
                {(p.tags || []).map((t) => <span key={t} className="pill">{t}</span>)}
              </div>
            </div>
          ))}

        </>
      )}
    </>
  )
}
