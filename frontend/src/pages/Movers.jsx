import React from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, Table, useAsync } from '../components/ui.jsx'

export default function Movers() {
  const m = useAsync(() => api.movers(30), [], 30000)
  const hurdle = m.data?.cost_hurdle_bps

  return (
    <>
      <h1>Movers</h1>
      {/* Banner was imported and never rendered, so when the request failed the
          page showed an empty table and blamed the universe. It failed every
          time: the old backend made 150 serial Coinbase calls (~20s) against a
          20s client deadline. Never let a fetch failure look like "no data". */}
      {m.err && <Banner kind="bad" title="movers could not load">{String(m.err)}</Banner>}
      <p className="sub">
        Ranked by absolute 24h move, with the cost of trading each one attached.<Info text="This is the screen you already use manually. 'move / cost' is how many times the day's move covers a round trip on that specific coin — below about 3x there is not enough room to be wrong in. The widest movers usually carry the widest spreads." />
      </p>
      <Card title={`ranked by |24h move| · required gross edge ${hurdle ? fmt.pct(hurdle / 100) : '—'} per round trip`}>
        <Table
          cols={[
            { key: 'symbol', label: 'symbol' },
            { key: 'change_24h_pct', label: '24h', num: true,
              render: (r) => <span className={r.change_24h_pct >= 0 ? 'pos' : 'neg'}>{fmt.pct(r.change_24h_pct)}</span> },
            { key: 'range_24h_pct', label: '24h range', num: true, render: (r) => fmt.pct(r.range_24h_pct) },
            { key: 'mid', label: 'price', num: true, render: (r) => (r.mid > 1 ? fmt.num(r.mid, 2) : fmt.px(r.mid)) },
            { key: 'quoted_spread_bps', label: 'spread', num: true,
              render: (r) => <span className={r.quoted_spread_bps > 30 ? 'neg' : 'mut'}>{fmt.bps(r.quoted_spread_bps, 1)}</span> },
            { key: 'dollar_volume_24h', label: '24h $ vol', num: true,
              render: (r) => (r.dollar_volume_24h ? `$${(r.dollar_volume_24h / 1e6).toFixed(1)}M` : '—') },
            { key: 'est_round_trip_bps', label: 'est. RH round trip', num: true,
              render: (r) => (r.est_round_trip_bps
                ? <span className={r.est_round_trip_bps > 150 ? 'neg' : 'mut'}>
                    {fmt.pct(r.est_round_trip_bps / 100)}
                  </span> : '—') },
            { key: 'move_vs_cost', label: 'move ÷ cost', num: true,
              render: (r) => (r.move_vs_cost
                ? <span className={r.move_vs_cost > 3 ? 'pos' : 'neg'}>{fmt.num(r.move_vs_cost, 1)}×</span>
                : '—') },
            { key: 'rh_confirmed', label: 'on RH?', info: 'Seeded from a best guess at Robinhood\'s list. Only marked confirmed once the Robinhood MCP itself returns the symbol. Live mode refuses an unconfirmed coin.',
              render: (r) => <span className={`pill ${r.rh_confirmed ? 'ok' : 'warn'}`}>{r.rh_confirmed ? 'confirmed' : 'unverified'}</span> },
          ]}
          rows={m.data?.rows || []}
          empty="run Universe refresh from the backend, or check the feed is reachable"
        />
      </Card>
    </>
  )
}
