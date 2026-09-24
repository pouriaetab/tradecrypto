import React, { useState } from 'react'
import { api, fmt } from '../lib/api.js'
import { Card, Info, Table, Banner, useAsync } from '../components/ui.jsx'

const bps = (v) => (v === null || v === undefined ? '—' : `${v.toFixed(0)} bps`)
const pct = (v) => (v === null || v === undefined ? '—' : `${(v / 100).toFixed(2)}%`)

export default function VenueLab() {
  const [edge, setEdge] = useState('')
  const [tpd, setTpd] = useState(2)
  const vs = useAsync(() => api.venues(), [])
  const cmp = useAsync(() => api.venueCompare(edge === '' ? undefined : Number(edge), Number(tpd) || 2), [edge, tpd])
  const pb = useAsync(() => api.priceBasis(90), [])
  const [sym, setSym] = useState('DOGE')
  const [mid, setMid] = useState('')
  const [msg, setMsg] = useState('')
  const [edit, setEdit] = useState({})
  const [rh, setRh] = useState(null)
  const [rhMsg, setRhMsg] = useState('')

  const rec = async () => {
    setMsg('')
    try {
      const r = await api.priceBasisRecord(sym, Number(mid))
      setMsg(r.error || r.reading); if (!r.error) { setMid(''); pb.reload() }
    } catch (e) { setMsg(String(e)) }
  }
  const save = async (k) => {
    const f = edit[k] || {}
    const body = { key: k }
    for (const kk of ['maker_bps', 'taker_bps', 'spread_pct'])
      if (f[kk] !== undefined && f[kk] !== '') body[kk] = Number(f[kk])
    if (f.verified !== undefined) body.verified = f.verified
    try { await api.venueUpdate(body); setEdit((e) => ({ ...e, [k]: {} })); vs.reload(); cmp.reload() }
    catch (e) { setMsg(String(e)) }
  }

  const c = cmp.data
  return (
    <>
      <h1>Venue Lab</h1>
      <p className="sub">
        What execution costs, and whether the edge survives it.
        <Info text="The edge audit measured 1–23 bps of gross edge per trade on this operator's own data. Robinhood's round trip is 192 bps. This page is where that comparison is made with numbers instead of opinions." />
      </p>

      {c && (
        <Banner kind={c.rows?.some((r) => r.profitable) ? 'info' : 'warn'} title="the arithmetic">
          {c.verdict}
          <div className="mut" style={{ marginTop: 4 }}>
            edge used: <b>{c.gross_edge_bps.toFixed(1)} bps</b> per trade · {c.edge_source}
          </div>
        </Banner>
      )}

      <Card title="cost comparison">
        <div className="filters" style={{ marginBottom: 8 }}>
          <label>
            gross edge
            <Info text="Basis points of price move the strategy captures BEFORE execution cost. Blank uses the measured 12-hour figure from research/edge_audit.py. Type a bigger number to ask what edge you would need." />
          </label>
          <input value={edge} onChange={(e) => setEdge(e.target.value)}
                 placeholder={c ? c.measured.unconditional_12h_bps : '23.3'} style={{ width: 80 }} /> bps
          <label>trades/day</label>
          <input value={tpd} onChange={(e) => setTpd(e.target.value)} style={{ width: 60 }} />
          <span className="mut">on ${c?.assumptions?.equity_usd ?? 500}</span>
        </div>
        <Table
          cols={[
            { key: 'venue', label: 'venue' },
            { key: 'style', label: 'order type',
              info: 'A taker order crosses the spread now. A maker order rests and waits, and usually costs about half. For a 6–12 hour hold, waiting is cheap.' },
            { key: 'round_trip_bps', label: 'round trip', num: true,
              info: 'In and out, all-in. Robinhood is a spread; the exchanges are a fee twice plus the book’s own spread.',
              render: (r) => `${bps(r.round_trip_bps)} (${pct(r.round_trip_bps)})` },
            { key: 'net_bps_per_trade', label: 'net / trade', num: true,
              render: (r) => <span className={r.profitable ? 'pos' : 'neg'}>{r.net_bps_per_trade.toFixed(1)} bps</span> },
            { key: 'edge_multiple_needed', label: 'edge needed', num: true,
              info: 'How many times bigger the gross edge would have to be for this venue to break even.',
              render: (r) => (r.edge_multiple_needed ? `${r.edge_multiple_needed.toFixed(0)}×` : '—') },
            { key: 'daily_usd', label: 'per day', num: true,
              render: (r) => <span className={r.daily_usd >= 0 ? 'pos' : 'neg'}>{fmt.usd(r.daily_usd)}</span> },
            { key: 'verified', label: 'checked',
              info: 'Whether YOU confirmed this against a real account statement. Unverified rows come from public fee pages that disagreed with each other.',
              render: (r) => <span className={`pill ${r.verified ? 'ok' : 'warn'}`}>{r.verified ? 'verified' : 'unverified'}</span> },
          ]}
          rows={c?.rows || []} />
      </Card>

      <Card title="the fee schedules — edit these against your own statement">
        <div className="hint">
          Kraken's own page and two comparison sites disagreed by a factor of three at the
          entry tier while this was written. Nothing here is trusted until you check it.
        </div>
        {(vs.data?.venues || []).map((v) => (
          <div key={v.key} className="rowblock">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <div style={{ maxWidth: '58%' }}>
                <b>{v.name}</b>
                <span className={`pill ${v.verified ? 'ok' : 'warn'}`} style={{ marginLeft: 8 }}>
                  {v.verified ? 'verified' : 'unverified'}
                </span>
                {!v.supports_limit_orders &&
                  <span className="pill" style={{ marginLeft: 6 }}>no limit orders</span>}
                <div className="mut" style={{ fontSize: 12 }}>{v.note}</div>
                <div className="mut" style={{ fontSize: 11, marginTop: 2 }}>source: {v.source}</div>
              </div>
              <div className="row">
                {v.model === 'spread' ? (
                  <><label>spread %/side</label>
                    <input defaultValue={v.spread_pct} style={{ width: 70 }}
                           onChange={(e) => setEdit((s) => ({ ...s, [v.key]: { ...s[v.key], spread_pct: e.target.value } }))} /></>
                ) : (
                  <><label>maker bps</label>
                    <input defaultValue={v.maker_bps} style={{ width: 60 }}
                           onChange={(e) => setEdit((s) => ({ ...s, [v.key]: { ...s[v.key], maker_bps: e.target.value } }))} />
                    <label>taker bps</label>
                    <input defaultValue={v.taker_bps} style={{ width: 60 }}
                           onChange={(e) => setEdit((s) => ({ ...s, [v.key]: { ...s[v.key], taker_bps: e.target.value } }))} /></>
                )}
                <label><input type="checkbox" defaultChecked={v.verified}
                              onChange={(e) => setEdit((s) => ({ ...s, [v.key]: { ...s[v.key], verified: e.target.checked } }))} /> checked</label>
                <button className="why" onClick={() => save(v.key)}>save</button>
              </div>
            </div>
            <div className="mut" style={{ fontSize: 12 }}>
              round trip — taker {bps(v.round_trip_taker_bps)}
              {v.round_trip_maker_bps !== null && <> · maker {bps(v.round_trip_maker_bps)}</>}
            </div>
          </div>
        ))}
      </Card>

      <Card title="Robinhood API — connect it and stop assuming">
        <div className="hint">
          Robinhood has a REST API with market data, holdings and orders. No language
          model, so no tokens. Setup is in <code>docs/ROBINHOOD_API.md</code>.
          <Info text="Three things only Robinhood can tell us: which coins is_api_tradable actually allows, the real bid/ask per coin instead of a 0.95% assumption, and the estimated fill for a given size." />
        </div>
        <div className="row" style={{ marginBottom: 6 }}>
          <button onClick={async () => { setRh(null); setRh(await api.rhProbe()) }}>check connection</button>
          <button className="why" disabled={!rh?.reachable}
                  onClick={async () => setRhMsg(JSON.stringify(await api.rhSyncUniverse()).slice(0, 300))}>
            sync tradable coins</button>
          <button className="why" disabled={!rh?.reachable}
                  onClick={async () => setRhMsg(JSON.stringify(await api.rhMeasureSpreads()).slice(0, 300))}>
            measure real spreads</button>
          <button className="primary" disabled={!rh?.reachable}
                  onClick={async () => setRhMsg(JSON.stringify(await api.rhRoutingCost(), null, 1).slice(0, 1800))}>
            which route is cheapest?</button>
        </div>
        {rh && (
          <div className={`hint-more ${rh.reachable ? '' : 'neg'}`}>
            {rh.configured
              ? (rh.reachable
                  ? <>connected · account {rh.account?.account_number} · buying power {rh.account?.buying_power} {rh.account?.buying_power_currency}</>
                  : <>credentials present but the call failed — {rh.error}</>)
              : <>not configured. {rh.next_step}</>}
            {rh.pynacl_installed === false && <div className="neg">PyNaCl missing — {rh.fix}</div>}
          </div>
        )}
        {rhMsg && <pre className="scroll" style={{ maxHeight: 160 }}>{rhMsg}</pre>}
        <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>
          Robinhood v2 fee tiers (exchange routing): 0.95% taker / 0.50% maker under
          $10K of 30-day volume, falling to 0.25% / 0.125% above $50K. The v2 API is
          charged the <b>taker</b> rate until maker/taker finishes rolling out, and v1
          API orders use market-maker routing with the spread and do not count toward
          tier volume at all.
        </div>
      </Card>

      <Card title="does our price match Robinhood's?">
        <div className="hint">{pb.data?.how}</div>
        {pb.data && (
          <Banner kind={(pb.data.median_abs_bps ?? 0) <= pb.data.tolerance_bps ? 'info' : 'warn'}
                  title="basis check">{pb.data.verdict}</Banner>
        )}
        <div className="filters" style={{ marginBottom: 8 }}>
          <label>coin</label>
          <input value={sym} onChange={(e) => setSym(e.target.value.toUpperCase())} style={{ width: 80 }} />
          <label>Robinhood mid</label>
          <input value={mid} onChange={(e) => setMid(e.target.value)} placeholder="0.090569" style={{ width: 120 }} />
          <button className="primary" onClick={rec}>record now</button>
          <span className="mut">{msg}</span>
        </div>
        <Table
          cols={[
            { key: 'ts', label: 'when', render: (r) => fmt.time(r.ts) },
            { key: 'symbol', label: 'coin' },
            { key: 'rh_mid', label: 'Robinhood mid', num: true, render: (r) => fmt.px(r.rh_mid) },
            { key: 'feed_mid', label: 'our feed', num: true, render: (r) => fmt.px(r.feed_mid) },
            { key: 'lag_s', label: 'time apart', num: true,
              info: 'How far our nearest price was from your observation. Prices move — a large basis on a stale observation is measuring the clock, not the venue.',
              render: (r) => (r.lag_s === null ? '—' : `${(r.lag_s / 60).toFixed(0)}m`) },
            { key: 'basis_bps', label: 'basis', num: true,
              info: '(our price − Robinhood) / Robinhood, in bps. Near zero means the research is measuring the market we would actually trade.',
              render: (r) => (r.basis_bps === null ? '—' :
                <span className={Math.abs(r.basis_bps) <= 25 ? 'pos' : 'neg'}>{r.basis_bps.toFixed(1)}</span>) },
          ]}
          rows={pb.data?.rows || []} empty="no observations yet — record one above" />
      </Card>
    </>
  )
}
