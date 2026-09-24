import React, { useState } from 'react'
import { api } from '../lib/api.js'
import { Banner, Card, Info, useAsync } from '../components/ui.jsx'

/* Every external API this app calls, and the RAW rows each one returns.
 *
 * Asked for several times: "I want to see what data each source API is actually
 * giving us, as a table." Not a description of the API -- the bytes. Every
 * sample here is fetched live on click and rendered with nothing renamed,
 * rounded, or filled in. A missing field from the provider stays missing. */

function RawTable({ data }) {
  if (!data) return null
  if (data.error) return <Banner kind="bad" title="the source returned an error">{data.error}</Banner>
  const cols = data.columns || []
  if (!cols.length) return <p className="mut">no rows came back</p>
  return (
    <div style={{ overflowX: 'auto', maxHeight: 460, overflowY: 'auto' }}>
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {(data.rows || []).map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className="mono" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {v === null || v === undefined
                    ? <span className="mut">null</span>
                    : typeof v === 'object' ? JSON.stringify(v) : String(v)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Sources() {
  const cat = useAsync(() => api.dataSources(), [])
  const [pick, setPick] = useState(null)        // {kind:'api'|'table', key}
  const [product, setProduct] = useState('BTC-USD')
  const [sample, setSample] = useState(null)
  const [busy, setBusy] = useState(false)
  const [sql, setSql] = useState('SELECT symbol, COUNT(*) n, MAX(ts) newest\nFROM bars\nWHERE granularity = 3600\nGROUP BY symbol\nORDER BY n DESC')
  const [qres, setQres] = useState(null)
  const [qbusy, setQbusy] = useState(false)

  const load = async (kind, key) => {
    setPick({ kind, key }); setBusy(true); setSample(null)
    try {
      setSample(kind === 'api'
        ? await api.dataSourceSample(key, product)
        : await api.dataSourcePeek(key))
    } catch (e) {
      setSample({ error: e.message })
    } finally { setBusy(false) }
  }

  const runSql = async () => {
    setQbusy(true)
    try { setQres(await api.dataSourceQuery(sql)) }
    catch (e) { setQres({ error: e.message }) }
    finally { setQbusy(false) }
  }

  const d = cat.data
  return (
    <>
      <h1>Sources</h1>
      <p className="sub">
        Every API this app calls, and exactly what it returns.
        <Info text="Each sample is fetched live when you click it and shown raw — same field names, same values, same nulls the provider sent. If a column looks wrong here, it is wrong at the source, not in our parsing. The lower half is what we STORED from those feeds, so you can compare the two." />
      </p>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 300px) minmax(0, 1fr)', gap: 10, alignItems: 'start' }}>
        <div>
          <Card title="external apis">
            {(d?.sources || []).map((s) => (
              <div key={s.key} style={{ marginBottom: 5 }}>
                <button className={pick?.key === s.key ? 'primary' : ''}
                        style={{ width: '100%', textAlign: 'left' }}
                        onClick={() => load('api', s.key)}>
                  {s.key}
                </button>
                <div className="mut" style={{ fontSize: 10.5, padding: '1px 4px 3px' }}>
                  {s.provider} · {s.auth === 'none' ? 'no key' : 'signed'} → {s.feeds}
                </div>
              </div>
            ))}
            <div className="row" style={{ marginTop: 6 }}>
              <span className="mut" style={{ fontSize: 11 }}>symbol</span>
              <input value={product} onChange={(e) => setProduct(e.target.value)}
                     style={{ width: 110 }} placeholder="BTC-USD" />
            </div>
          </Card>

          <Card title="what we stored">
            {(d?.local || []).map((t) => (
              <button key={t.table}
                      className={pick?.key === t.table ? 'primary' : ''}
                      style={{ width: '100%', textAlign: 'left', marginBottom: 3 }}
                      onClick={() => load('table', t.table)}>
                {t.table} <span className="mut">· {t.rows.toLocaleString()} rows</span>
              </button>
            ))}
          </Card>
        </div>

        <div>
          <Card title={pick ? `${pick.key} — raw` : 'pick a source on the left'}>
            {busy && <p className="mut">fetching live…</p>}
            {sample && !busy && (
              <>
                <div className="row" style={{ marginBottom: 6 }}>
                  {sample.request_url && (
                    <span className="mono mut" style={{ fontSize: 11, wordBreak: 'break-all' }}>
                      {sample.request_url}
                    </span>
                  )}
                  {sample.elapsed_ms != null && <span className="pill">{sample.elapsed_ms} ms</span>}
                  <span className="pill">{sample.row_count ?? 0} rows</span>
                  {sample.columns && <span className="pill">{sample.columns.length} columns</span>}
                </div>
                {sample.note && <div className="step-detail" style={{ marginBottom: 6 }}>{sample.note}</div>}
                <RawTable data={sample} />
                {sample.meta_from_provider && (
                  <details style={{ marginTop: 8 }}>
                    <summary className="mut" style={{ cursor: 'pointer', fontSize: 11 }}>
                      provider metadata block
                    </summary>
                    <pre style={{ fontSize: 11, whiteSpace: 'pre-wrap' }}>
                      {JSON.stringify(sample.meta_from_provider, null, 1)}
                    </pre>
                  </details>
                )}
              </>
            )}
            {!sample && !busy && (
              <p className="mut">
                Click any API to fetch it live, or any stored table to see the newest rows.
              </p>
            )}
          </Card>

          <Card title="explore — read-only SQL over the stored tables">
            <textarea value={sql} onChange={(e) => setSql(e.target.value)}
                      rows={4} spellCheck={false}
                      style={{ width: '100%', fontFamily: 'var(--mono)', fontSize: 12 }} />
            <div className="row" style={{ marginTop: 5 }}>
              <button className="primary" disabled={qbusy} onClick={runSql}>
                {qbusy ? 'running…' : 'run'}
              </button>
              <span className="mut" style={{ fontSize: 11 }}>
                SELECT only, one statement, capped at 200 rows
                <Info text="Read-only on purpose. This is the same database that holds your live trading record, so nothing here can write to it." />
              </span>
            </div>
            {qres && <div style={{ marginTop: 8 }}><RawTable data={qres} /></div>}
          </Card>
        </div>
      </div>
    </>
  )
}
