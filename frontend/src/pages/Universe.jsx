import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Stat, Table, useAsync } from '../components/ui.jsx'

const ROLE = { core: 'ok', tradeable: 'ok', watch: 'warn', excluded: 'bad' }

export default function Universe({ onModel }) {
  const sel = useAsync(() => api.universeSelection(true), [], 60000)
  const hist = useAsync(() => api.universeHistory(40), [])
  const macro = useAsync(() => api.macro(), [])
  const pmk = useAsync(() => api.predictionMarkets(), [], 300000)
  const corr = useAsync(() => api.macroCorrelations(365), [])
  const [busy, setBusy] = useState(false)
  const [expand, setExpand] = useState(null)
  // What Robinhood actually offers vs what we can price vs what we trade. This
  // ran silently on boot and logged one uninformative line, so the difference
  // between "Robinhood lists 88 coins" and "this desk trades 23" was invisible.
  const [syncing, setSyncing] = useState(false)
  const [sync, setSync] = useState(null)
  // Recorded by adopt_from_robinhood(), which runs on every boot. Without this
  // the tiles below could only ever be filled by pressing the button, in this
  // browser session, and then they read field names the button's endpoint does
  // not return -- two dashes and two zeros, permanently.
  const counts = useAsync(() => api.rhListCounts(), [], 300000)

  const d = sel.data
  const shown = sync || counts.data || {}

  return (
    <>
      <h1>Universe</h1>
      <p className="sub">
        Which coins we track and trade, decided by gates rather than a list.
        <Info text="The original 50-coin list was hand-written from memory — it could not notice that FIL is untradeable on Robinhood, drop a coin that went quiet, or pick up a new listing. Roles are now recomputed from liquidity, cost, movement and data quality, with hysteresis so borderline coins do not churn in and out." />
      </p>

      {d && !d.robinhood_synced && (
        <Banner kind="warn" title="Robinhood not synced — every coin is assumed tradeable">
          Until you run the sync, a coin like FIL that is not on Robinhood is still treated as
          tradeable. See docs/ROBINHOOD_SYNC.md.
        </Banner>
      )}

      <Card>
        <div className="row">
          {d && <>
            <span className="pill ok">{d.counts.core} core</span>
            <span className="pill ok">{d.counts.tradeable} tradeable</span>
            <span className="pill warn">{d.counts.watch} watch</span>
          </>}
          <div className="spacer" />
          <button disabled={syncing} title="Ask Robinhood what it will actually trade through the API, and report how many of those we can price."
                  onClick={async () => {
                    setSyncing(true); setSync(null)
                    // runs automatically on every app start; this is just "do it again now"
                    try { setSync(await api.rhSyncUniverse()) }
                    catch (e) { setSync({ error: e.message }) }
                    finally { setSyncing(false); sel.reload() }
                  }}>{syncing ? 'asking Robinhood…' : 'recheck Robinhood list now'}</button>
          <button className="primary" disabled={busy} onClick={async () => {
            setBusy(true)
            try { await api.universeReview(); sel.reload(); hist.reload() } finally { setBusy(false) }
          }}>{busy ? 'recomputing…' : 'recompute roles now'}</button>
        </div>
        {/* `shown` prefers a fresh button press but falls back to the recorded
            check, so the tiles are populated the moment the page opens. The app
            runs adopt_from_robinhood() on every boot, so there is always a
            recent number to show. */}
        {(sync || counts.data) && ((sync || counts.data).error
          ? <Banner kind="bad" title="Could not reach Robinhood">{sync.error}</Banner>
          : <div className="row" style={{ gap: 18, marginTop: 10, flexWrap: 'wrap' }}>
              <Stat label="pairs Robinhood returned" value={shown.pairs_returned_by_robinhood ?? '—'} />
              <Stat label="tradeable through the API" value={shown.api_tradable_on_robinhood ?? '—'} />
              <Stat label="coins we track and can trade" value={d?.counts ? (d.counts.core + d.counts.tradeable + d.counts.watch) : '—'} />
              <Stat label="we cannot price" value={(shown.tradable_but_feed_cannot_price || []).length} />
              {(shown.tradable_but_feed_cannot_price || []).length > 0 && (
                <div className="step-detail" style={{ flexBasis: '100%' }}>
                  Robinhood will trade these but our price feed has no product for them, so they
                  cannot be watched: {(shown.tradable_but_feed_cannot_price || []).join(', ')}
                </div>)}
            </div>)}
      </Card>

      <h2>Coins</h2>
      <Card>
        <Table
          cols={[
            { key: 'symbol', label: 'coin',
              render: (r) => <button className="linkish"
                onClick={() => setExpand(expand === r.symbol ? null : r.symbol)}>{r.symbol}</button> },
            { key: 'new_role', label: 'role',
              render: (r) => <span className={`pill ${ROLE[r.new_role] || ''}`}>{r.new_role}</span> },
            { key: 'score', label: 'score', num: true,
              render: (r) => <span className={r.score > 0.55 ? 'pos' : r.score < 0.35 ? 'neg' : ''}>
                {fmt.num(r.score, 2)}</span> },
            { key: 'dv', label: '$ vol/day', num: true,
              render: (r) => (r.metrics?.dollar_volume_day
                ? `$${(r.metrics.dollar_volume_day / 1e6).toFixed(1)}M` : '—') },
            { key: 'rt', label: 'round trip', num: true,
              render: (r) => fmt.pct(r.round_trip_bps / 100) },
            { key: 'vol', label: 'vol/hr', num: true,
              render: (r) => (r.metrics?.vol_bps ? fmt.bps(r.metrics.vol_bps, 0) : '—') },
            { key: 'rh_tradable', label: 'on RH',
              render: (r) => <span className={`pill ${r.rh_tradable ? '' : 'bad'}`}>
                {r.rh_tradable ? 'yes' : 'no'}</span> },
            { key: 'reason', label: 'why', render: (r) => <span className="mut">{r.reason}</span> },
          ]}
          rows={d?.rows || []} />
        {expand && d && (
          <div style={{ marginTop: 8 }}>
            <Table
              cols={[
                { key: 'gate', label: `${expand} — gate` },
                { key: 'passed', label: '',
                  render: (g) => <span className={`pill ${g.passed ? 'ok' : 'bad'}`}>{g.passed ? 'pass' : 'fail'}</span> },
                { key: 'actual', label: 'actual' },
                { key: 'required', label: 'required' },
                { key: 'score', label: 'score', num: true, render: (g) => fmt.num(g.score, 2) },
                { key: 'why', label: 'why this gate', render: (g) => <span className="mut">{g.why}</span> },
              ]}
              rows={(d.rows.find((r) => r.symbol === expand) || {}).gates || []} />
          </div>
        )}
      </Card>

      <h2>Role changes</h2>
      <Card>
        <Table
          cols={[
            { key: 'ts', label: 'when', render: (r) => fmt.time(r.ts) },
            { key: 'symbol', label: 'coin' },
            { key: 'from_role', label: 'from' },
            { key: 'to_role', label: 'to' },
            { key: 'score', label: 'score', num: true, render: (r) => fmt.num(r.score, 2) },
            { key: 'reason', label: 'why' },
          ]}
          rows={hist.data || []} empty="no role changes recorded yet" />
      </Card>

      <h2>What the crowd expects<Info text="Polymarket's crypto markets, fetched hourly. A same-day ladder — 'will Bitcoin be above $86,000 on the 21st' at $2,000 steps — is read as a distribution: the strike where the odds cross one half is the implied close, the 10%–90% strikes the implied range. Context for the regime read (BTC and ETH only, day and month horizons). Not an entry input for any strategy; nothing in the execution path reads it." /></h2>
      <Card right={<button onClick={() => api.predictionMarketsFetch().then(() => pmk.reload())}>refresh</button>}>
        {pmk.err ? <div className="neg">{pmk.err}</div>
          : !pmk.data?.available ? <div className="mut">{pmk.data?.why || 'loading…'}</div>
          : (
            <>
              <div className="stats">
                {Object.entries(pmk.data.today || {}).map(([asset, t]) => (
                  <Stat key={asset} label={`${asset} today (resolves ${t.resolves})`}
                    value={`$${fmt.num(t.median, 0)}`}
                    meta={`implied close; 10–90% range $${fmt.num(t.p10, 0)} – $${fmt.num(t.p90, 0)}, ${t.strikes} strikes`} />
                ))}
                {!Object.keys(pmk.data.today || {}).length && <div className="mut">no same-day ladder in this fetch</div>}
              </div>
              <Table
                cols={[
                  { key: 'question', label: 'market' },
                  { key: 'p_yes', label: 'odds', num: true, render: (r) => fmt.pct(r.p_yes * 100, 1) },
                  { key: 'volume24h', label: '24h volume', num: true, render: (r) => `$${fmt.num(r.volume24h, 0)}` },
                  { key: 'end_date', label: 'resolves' },
                ]}
                rows={pmk.data.longer_dated || []} empty="no month or year markets in this fetch" />
              <div className="mut" style={{ marginTop: 6 }}>{pmk.data.note}</div>
            </>
          )}
      </Card>

      <h2>Outside crypto</h2>
      <Card right={<button onClick={() => api.macroFetch().then(() => { macro.reload(); corr.reload() })}>refresh</button>}>
        <Table
          cols={[
            { key: 'series', label: 'series',
              render: (r) => <span>{r.label}{r.why && <Info text={r.why} />}</span> },
            { key: 'close', label: 'last', num: true, render: (r) => fmt.num(r.close, 2) },
            { key: 'change_5d_pct', label: '5-day', num: true,
              render: (r) => <span className={(r.change_5d_pct ?? 0) >= 0 ? 'pos' : 'neg'}>
                {fmt.pct(r.change_5d_pct)}</span> },
            { key: 'date', label: 'as of' },
          ]}
          rows={macro.data?.rows || []} empty="press refresh to load" />
      </Card>

      {corr.data?.available && (
        <Card title="correlation with BTC — measured, not assumed">
          <Table
            cols={[
              { key: 'label', label: 'series' },
              { key: 'n', label: 'days', num: true },
              { key: 'rho', label: 'correlation', num: true, render: (r) => fmt.num(r.rho, 3) },
              { key: 'ci95', label: '95% CI', num: true,
                render: (r) => (r.ci95 ? `${fmt.num(r.ci95[0], 2)} to ${fmt.num(r.ci95[1], 2)}` : '—') },
              { key: 'stable', label: 'stable',
                render: (r) => <span className={`pill ${r.stable ? 'ok' : 'warn'}`}>{r.stable ? 'yes' : 'no'}</span> },
              { key: 'verdict', label: 'verdict', render: (r) => <span className="mut">{r.verdict}</span> },
            ]}
            rows={corr.data.rows} />
          <div className="hint">{corr.data.caveat}</div>
        </Card>
      )}
    </>
  )
}
