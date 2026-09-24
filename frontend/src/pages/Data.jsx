import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

const GRANS = [
  [86400, '1 day — back to each coin\'s listing, ~10 years'],
  [3600, '1 hour — several years, the workhorse'],
  [900, '15 minutes — typically 1–2 years'],
  [60, '1 minute — only weeks; nobody backfills this'],
]

/* The database as a tree. One node per table, opened to show its columns;
 * a column that names another table (trade_id -> trades) is a branch you can
 * follow. Built from PRAGMA on the live file, so it is the schema that is
 * actually there, not a drawing of the one somebody remembers. */
function SchemaTree({ data }) {
  const [open, setOpen] = useState(() => new Set())
  const [q, setQ] = useState('')
  if (!data) return <div className="mut">reading the schema…</div>
  const toggle = (t) => setOpen((s) => { const n = new Set(s); n.has(t) ? n.delete(t) : n.add(t); return n })
  const incoming = {}
  for (const l of data.links) (incoming[l.to] = incoming[l.to] || []).push(l)
  const tables = data.tables.filter((t) => !q || t.table.includes(q.toLowerCase())
    || t.columns.some((c) => c.name.includes(q.toLowerCase())))
  const all = () => setOpen(new Set(data.tables.map((t) => t.table)))
  const none = () => setOpen(new Set())
  return (
    <>
      <div className="filters">
        <input placeholder="find a table or column" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} />
        <button onClick={all}>expand all</button>
        <button onClick={none}>collapse all</button>
        <span className="mut" style={{ fontSize: 11 }}>{data.tables.length} tables · {data.links.length} inferred links</span>
      </div>
      <div className="schema-tree">
        {tables.map((t) => {
          const isOpen = open.has(t.table) || !!q
          return (
            <div key={t.table} className="schema-node">
              <div className="schema-row" onClick={() => toggle(t.table)}>
                <span className="schema-caret">{isOpen ? '▾' : '▸'}</span>
                <b>{t.table}</b>
                <span className="mut">{t.rows == null ? '' : `${t.rows.toLocaleString()} rows`} · {t.columns.length} columns
                  {t.indexes.length ? ` · ${t.indexes.length} index${t.indexes.length === 1 ? '' : 'es'}` : ''}</span>
                {!!(incoming[t.table] || []).length && (
                  <span className="mut" title={incoming[t.table].map((l) => `${l.from}.${l.column}`).join(', ')}>
                    ← {incoming[t.table].length} table{incoming[t.table].length === 1 ? '' : 's'} point here
                  </span>
                )}
              </div>
              {isOpen && (
                <div className="schema-children">
                  {t.columns.map((c) => (
                    <div key={c.name} className="schema-col">
                      <span className="schema-branch">├─</span>
                      <span className={c.pk ? 'pos' : ''}>{c.name}</span>
                      <span className="mut">{c.type.toLowerCase()}{c.pk ? ' · primary key' : ''}{c.notnull && !c.pk ? ' · not null' : ''}
                        {c.default != null ? ` · default ${c.default}` : ''}</span>
                      {c.ref && (
                        <button className="why" style={{ padding: '0 6px', fontSize: 11 }}
                                title="inferred from the column name — not an enforced foreign key"
                                onClick={(e) => { e.stopPropagation(); setOpen((s) => new Set([...s, c.ref])); setQ('') }}>
                          → {c.ref}
                        </button>
                      )}
                    </div>
                  ))}
                  {t.indexes.map((i) => (
                    <div key={i.name} className="schema-col">
                      <span className="schema-branch">└─</span>
                      <span className="mut">index {i.unique ? '(unique) ' : ''}{i.name}: {i.columns.join(', ')}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
      <div className="mut" style={{ fontSize: 11, marginTop: 8 }}>{data.note}</div>
    </>
  )
}

/* The whole app as one tree. Three roots -- pages, jobs, tables -- because
 * "how does the app fit together" is a different question from each side:
 * a page fans out to the routes it calls and the tables behind them; a job
 * to the modules it runs; a table to everyone who touches it. */
function Branch({ label, meta, children, depth = 0, defaultOpen = false, count }) {
  const [open, setOpen] = useState(defaultOpen)
  const has = React.Children.count(children) > 0
  return (
    <div className="arch-node" style={{ paddingLeft: depth ? 18 : 0 }}>
      <div className={`schema-row${has ? '' : ' leaf'}`} onClick={() => has && setOpen((o) => !o)}>
        <span className="schema-caret">{has ? (open ? '▾' : '▸') : '·'}</span>
        <b>{label}</b>
        {count != null && <span className="mut">{count}</span>}
        {meta && <span className="mut">{meta}</span>}
      </div>
      {open && has && <div>{children}</div>}
    </div>
  )
}

function ArchitectureTree({ data }) {
  const [view, setView] = useState('pages')
  if (!data) return <div className="mut">reading the source…</div>
  const c = data.counts
  return (
    <>
      <div className="filters">
        <div className="seg">
          {[['pages', `pages (${c.pages})`], ['jobs', `jobs (${c.jobs})`], ['tables', `tables (${c.tables})`]].map(([k, l]) => (
            <button key={k} className={view === k ? 'on' : ''} onClick={() => setView(k)}>{l}</button>
          ))}
        </div>
        <span className="mut" style={{ fontSize: 11 }}>{c.routes} routes · {c.modules} backend modules</span>
      </div>
      <div className="schema-tree">
        {view === 'pages' && data.pages.map((p) => (
          <Branch key={p.page} label={p.page} count={`${p.endpoints.length} call${p.endpoints.length === 1 ? '' : 's'}`}
                  meta={p.tables.length ? `→ ${p.tables.join(', ')}` : ''}>
            {p.endpoints.map((e) => (
              <Branch key={e.call} depth={1} label={`api.${e.call}()`}
                      meta={e.route ? `${e.method} ${e.route} → ${e.handler}()` : `${e.path || '?'} (no route matched)`}>
                {e.modules.map((m) => (
                  <Branch key={m} depth={2} label={m.replace(/^app\./, '')}
                          meta={(data.tables && Object.entries(data.tables).filter(([, v]) => v.modules.includes(m)).map(([t]) => t).join(', ')) || ''} />
                ))}
              </Branch>
            ))}
          </Branch>
        ))}
        {view === 'jobs' && data.jobs.map((j) => (
          <Branch key={j.job} label={j.job} count={j.interval ? `every ${j.interval.replace(/\s*\*\s*/g, '×').toLowerCase()}` : ''}
                  meta={j.what}>
            {j.modules.map((m) => (
              <Branch key={m} depth={1} label={m.replace(/^app\./, '')}
                      meta={(Object.entries(data.tables).filter(([, v]) => v.modules.includes(m)).map(([t]) => t).join(', ')) || ''} />
            ))}
          </Branch>
        ))}
        {view === 'tables' && Object.entries(data.tables).map(([t, v]) => (
          <Branch key={t} label={t} count={`${v.modules.length} modules · ${v.pages.length} pages · ${v.jobs.length} jobs`}>
            <Branch depth={1} label="modules" meta={v.modules.map((m) => m.replace(/^app\./, '')).join(', ') || '—'} />
            <Branch depth={1} label="pages" meta={v.pages.join(', ') || '—'} />
            <Branch depth={1} label="jobs" meta={v.jobs.join(', ') || '—'} />
          </Branch>
        ))}
      </div>
      <div className="mut" style={{ fontSize: 11, marginTop: 8 }}>{data.note}</div>
    </>
  )
}

/** Who is holding the one database lock, and what every thread is doing.
 *
 * 2026-09-21 10:10: /mode took 27 s, /health/report (no database) took 24 s,
 * one tick took 365 s, and every reading on every tab said "slow" without
 * saying WHO. This is the who: the lock's current holder, how long callers
 * have waited for it since boot, the tick profile, and a stack per thread. */
function LockCard() {
  const th = useAsync(() => api.threads(), [], 5000)
  const [open, setOpen] = useState(false)
  const d = th.data
  if (th.err) return <Card title="Who holds the database lock"><div className="neg">{th.err}</div></Card>
  if (!d) return <Card title="Who holds the database lock"><div className="mut">reading…</div></Card>
  const h = d.lock_holder || {}
  const w = d.lock_waits || {}
  const tp = d.tick_profile_last || {}
  const busy = (w.n || 0) > 0 ? (w.total_wait_s || 0) / (w.n || 1) : 0
  return (
    <Card title="Who holds the database lock" right={<button className="why" onClick={() => setOpen(!open)}>{open ? 'hide threads' : 'show threads'}</button>}>
      <div className="stats">
        <Stat label="held by" value={h.thread || 'nobody'} meta={h.what ? `${(h.held_for_s || 0).toFixed(2)}s — ${String(h.what).slice(0, 90)}` : 'free right now'} />
        <Stat label="waits since boot" value={(w.n || 0).toLocaleString()} meta={`${(w.total_wait_s || 0).toFixed(0)}s waited in total; longest ${(w.max_wait_s || 0).toFixed(1)}s`} tone={(w.max_wait_s || 0) > 10 ? 'warn' : undefined} />
        <Stat label="mean wait" value={`${(busy * 1000).toFixed(0)} ms`} meta="per acquisition; over 500 ms means callers are queueing" tone={busy > 0.5 ? 'warn' : undefined} />
        <Stat label="last tick" value={tp.last_s != null ? `${tp.last_s.toFixed(1)}s` : '—'} meta={tp.n ? `mean ${tp.total_mean_s.toFixed(1)}s, p95 ${tp.total_p95_s.toFixed(1)}s over ${tp.n} ticks` : (tp.note || '')} tone={tp.last_s > 60 ? 'warn' : undefined} />
      </div>
      {open && (
        <div style={{ marginTop: 10, fontFamily: 'ui-monospace, monospace', fontSize: 11 }}>
          {Object.entries(d.stacks || {}).map(([name, st]) => (
            <div key={name} style={{ marginBottom: 6 }}>
              <div><strong>{name}</strong></div>
              {st.map((f, i) => <div key={i} className="mut" style={{ paddingLeft: 12 }}>{f}</div>)}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function Data({ onModel }) {
  const arch = useAsync(() => api.architecture(), [], 600000)
  const schema = useAsync(() => api.schema(), [], 120000)
  const cov = useAsync(() => api.coverage(), [], 15000)
  const st = useAsync(() => api.backfillStatus(), [], 3000)
  const store = useAsync(() => api.storage(80), [])
  const [gran, setGranRaw] = useState(3600)
  const [days, setDays] = useState(1460)
  const setGran = (g) => {
    setGranRaw(g)
    setDays(g === 60 ? 7 : 1460)   // minute history only goes back weeks
  }
  const [msg, setMsg] = useState(null)

  const [sym, setSym] = useState('BTC')
  const [rawGran, setRawGran] = useState(3600)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [raw, setRaw] = useState(null)

  const running = st.data?.running
  const p = st.data?.progress

  const startFill = async () => {
    try {
      const d = await api.backfillStart(gran, days)
      setMsg(d.started ? d.note : d.reason)
      st.reload()
    } catch (e) { setMsg(e.message) }
  }

  const loadRaw = async () => {
    const ts = (s) => (s ? new Date(`${s}T00:00:00`).getTime() / 1000 : null)
    setRaw(await api.rawData(sym, rawGran, ts(from), ts(to)))
  }

  return (
    <>
      <h1>Data</h1>
      <p className="sub">
        Years of history, for free, by walking the feed backwards.<Info text="A live feed serves only a few hundred recent candles, which is why every strategy starts out data-starved. Daily goes back to each coin's listing, hourly gives several years, 15-minute one to two years. One-minute cannot be backfilled from anywhere — it is collected going forward only, which is a real constraint on the fast strategies." />
      </p>

      <h2>Backfill</h2>
      <Card right={<button className="why" onClick={() => onModel('historical_data')}>how this works</button>}>
        <div className="filters">
          <label>resolution</label>
          <select value={gran} onChange={(e) => setGran(Number(e.target.value))}>
            {GRANS.map(([g, label]) => <option key={g} value={g}>{label}</option>)}
          </select>
          <label>go back</label>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {gran === 60
              ? <>
                  <option value={3}>3 days</option>
                  <option value={7}>1 week</option>
                  <option value={14}>2 weeks</option>
                  <option value={30}>1 month (as far as it goes)</option>
                </>
              : <>
                  <option value={90}>3 months</option>
                  <option value={365}>1 year</option>
                  <option value={730}>2 years</option>
                  <option value={1460}>4 years</option>
                  <option value={2920}>8 years</option>
                </>}
          </select>
          {gran === 60 && (
            <Info text="No public venue sells minute history — a few weeks is all that exists, and the request simply stops when the feed runs out. The only way to ever have a year of it is to keep collecting, which the 'minute_topup' job on the Automation page now does every 6 hours for the coins you actually trade." />
          )}
          <div className="spacer" />
          {running
            ? <button className="danger" onClick={() => api.backfillCancel().then(st.reload)}>cancel</button>
            : <button className="primary" onClick={startFill}>start backfill</button>}
        </div>
        {msg && <div className="mut" style={{ marginTop: 8 }}>{msg}</div>}
        {p && (
          <>
            <div className="progress" style={{ marginTop: 12 }}>
              <div className="progress-fill" style={{ width: `${p.pct || 0}%` }} />
            </div>
            <div className="row mono" style={{ marginTop: 8, fontSize: 12 }}>
              <span>{p.symbols_done}/{p.symbols_total} coins</span>
              <span>{(p.rows_written || 0).toLocaleString()} bars</span>
              <span>{(p.requests_made || 0).toLocaleString()} requests</span>
              {p.current_symbol && <span>now: {p.current_symbol}</span>}
              {p.eta_s != null && <span>eta {(p.eta_s / 60).toFixed(0)}m</span>}
              <div className="spacer" />
              <span className={running ? 'pos' : 'mut'}>{running ? 'running' : 'idle'}</span>
            </div>
            {p.errors?.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary className="mut">{p.errors.length} errors</summary>
                <pre style={{ fontSize: 11 }}>{p.errors.slice(0, 20).join('\n')}</pre>
              </details>
            )}
          </>
        )}
      </Card>

      <h2>What we hold</h2>
      <div className="grid c3">
        <Stat label="Total bars stored" small value={(cov.data?.total_rows || 0).toLocaleString()} />
        <Stat label="Database size" small
              value={`${((cov.data?.database_bytes || 0) / 1e6).toFixed(1)} MB`} />
        <Stat label="Resolutions" small value={(cov.data?.by_granularity || []).length} />
      </div>
      {(cov.data?.by_granularity || []).map((b) => (
        <Card key={b.granularity} title={`${b.label} — ${b.symbols} coins, ${b.rows.toLocaleString()} bars, ${b.span_days.toFixed(0)} days`}>
          <Table
            cols={[
              { key: 'symbol', label: 'coin' },
              { key: 'rows', label: 'bars', num: true, render: (r) => r.rows.toLocaleString() },
              { key: 'span_days', label: 'span', num: true, render: (r) => `${r.span_days.toFixed(0)}d` },
              { key: 'first_ts', label: 'from', render: (r) => fmt.time(r.first_ts) },
              { key: 'last_ts', label: 'to', render: (r) => fmt.time(r.last_ts) },
              { key: 'completeness_pct', label: 'complete', num: true,
                render: (r) => (r.completeness_pct == null ? '—'
                  : <span className={r.completeness_pct > 90 ? 'pos' : 'neg'}>{fmt.pct(r.completeness_pct, 0)}</span>) },
            ]}
            rows={b.per_symbol} />
        </Card>
      ))}

      <h2>Will this fit on my machine?<Info text="Projected for 80 coins using this database's measured bytes-per-row, not a textbook figure." /></h2>
      {store.data && (
        <Card>
          <Banner kind="info" title={`Measured: ${store.data.measured_bytes_per_row.toFixed(0)} bytes per bar in this database`}>
            {store.data.recommendation}
          </Banner>
          <Table
            cols={[
              { key: 'granularity', label: 'resolution' },
              { key: 'years', label: 'years', num: true },
              { key: 'rows', label: 'bars', num: true, render: (r) => r.rows.toLocaleString() },
              { key: 'megabytes', label: 'size', num: true,
                render: (r) => (r.megabytes > 1000
                  ? <span className="neg">{(r.megabytes / 1000).toFixed(1)} GB</span>
                  : <span className="pos">{r.megabytes.toFixed(0)} MB</span>) },
            ]}
            rows={store.data.projections} />

        </Card>
      )}

      <h2>Who holds the database lock<Info text="Every query in the app goes through one lock. When a page says the backend is slow, this is what is holding it and for how long, plus how long callers have queued since boot and what the engine's last tick cost. Show threads to see each thread's stack." /></h2>
      <LockCard />

      <h2>Data schema<Info text="Every table in the trade database, as a tree. Click a table to see its columns; click an arrow to follow a link to the table it points at." /></h2>
      <Card>
        {schema.err ? <div className="neg">{schema.err}</div> : <SchemaTree data={schema.data} />}
      </Card>

      <h2>How the app fits together<Info text="Read from the source, not drawn: which routes each page calls, which backend modules each route runs, which tables those modules read or write — and the same from the jobs' side and the tables' side." /></h2>
      <Card>
        {arch.err ? <div className="neg">{arch.err}</div> : <ArchitectureTree data={arch.data} />}
      </Card>

      <h2>Raw data browser</h2>
      <Card>
        <div className="filters">
          <label>coin</label>
          <input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} style={{ width: 90 }} />
          <label>resolution</label>
          <select value={rawGran} onChange={(e) => setRawGran(Number(e.target.value))}>
            {GRANS.map(([g, label]) => <option key={g} value={g}>{label.split(' — ')[0]}</option>)}
          </select>
          <label>from</label><input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          <label>to</label><input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          <button className="primary" onClick={loadRaw}>load</button>
        </div>
        {raw && (
          <>
            <div className="mut" style={{ marginBottom: 8 }}>
              {raw.n.toLocaleString()} bars of {raw.label} for {raw.symbol}
            </div>
            <Table
              cols={[
                { key: 'ts', label: 'time', render: (r) => fmt.time(r.ts) },
                { key: 'open', label: 'open', num: true, render: (r) => fmt.num(r.open, 6) },
                { key: 'high', label: 'high', num: true, render: (r) => fmt.px(r.high) },
                { key: 'low', label: 'low', num: true, render: (r) => fmt.px(r.low) },
                { key: 'close', label: 'close', num: true, render: (r) => fmt.px(r.close) },
                { key: 'volume', label: 'volume', num: true, render: (r) => fmt.num(r.volume, 2) },
                { key: 'source', label: 'source' },
              ]}
              rows={raw.rows.slice(-500)} />
          </>
        )}
      </Card>
    </>
  )
}
