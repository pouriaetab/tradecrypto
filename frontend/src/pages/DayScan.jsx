import React from 'react'
import { api, fmt } from '../lib/api.js'
import { Banner, Card, Info, useAsync } from '../components/ui.jsx'

const todayISO = () => {
  const p = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const g = (t) => p.find((x) => x.type === t).value
  return `${g('year')}-${g('month')}-${g('day')}`
}
const shift = (iso, days) => {
  const d = new Date(iso + 'T12:00:00Z')
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}
const pct = (v, n = 2) => (v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(n)}%`)
const mult = (v) => (v === null || v === undefined ? '—' : `${v.toFixed(2)}×`)
const price = (v) => (v === null || v === undefined ? '—' : `$${v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(4) : v.toFixed(2)}`)

// The exact status strings the scan emits, in the order they belong on screen:
// reachable first, unreachable last. Only statuses actually present on the day
// get a button, so this list never invents an option that has no rows.
const STATUS_ORDER = ['tradeable', 'on RH, not enabled', 'not on RH']
const F_ALL = 'all'
const F_RH = 'rh'

function Spark({ path, up, w = 104, h = 26 }) {
  if (!path || path.length < 2) return <span className="mut">—</span>
  const lo = Math.min(0, ...path); const hi = Math.max(0, ...path)
  const span = hi - lo || 1
  const x = (i) => 2 + (i * (w - 4)) / (path.length - 1)
  const y = (v) => h - 2 - ((v - lo) / span) * (h - 4)
  const d = 'M' + path.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('L')
  const area = `${d}L${x(path.length - 1).toFixed(1)},${y(lo).toFixed(1)}L${x(0).toFixed(1)},${y(lo).toFixed(1)}Z`
  const c = up ? 'var(--pos)' : 'var(--neg)'
  return (
    <svg className="ds-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
      {y(0) > 0 && y(0) < h && (
        <line x1="0" y1={y(0).toFixed(1)} x2={w} y2={y(0).toFixed(1)} stroke="var(--line)" strokeWidth="1" strokeDasharray="2 3" />
      )}
      <path d={area} fill={c} opacity="0.13" />
      <path d={d} fill="none" stroke={c} strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx={x(path.length - 1).toFixed(1)} cy={y(path[path.length - 1]).toFixed(1)} r="2.3" fill={c} />
    </svg>
  )
}


function CoinChart({ coin, day }) {
  const [hover, setHover] = React.useState(null)
  const W = 860, H = 260, VH = 58, PADL = 54, PADR = 12, PADT = 12, PADB = 20
  const path = coin.path || []
  const vols = coin.path_volume || []
  const times = coin.path_times || []
  if (path.length < 2) return <p className="mut">no intraday bars for this day</p>

  const lo = Math.min(...path), hi = Math.max(...path)
  const span = (hi - lo) || 1
  const pad = span * 0.08
  const yMin = lo - pad, yMax = hi + pad
  const x = (i) => PADL + (i * (W - PADL - PADR)) / (path.length - 1)
  const y = (v) => PADT + (1 - (v - yMin) / (yMax - yMin)) * (H - PADT - PADB - VH)
  const vMax = Math.max(...vols, 1e-9)
  const vy = (v) => (v / vMax) * (VH - 8)
  const base = H - PADB
  const up = coin.move >= 0
  const col = up ? 'var(--pos)' : 'var(--neg)'
  const d = 'M' + path.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join('L')
  const area = `${d}L${x(path.length - 1).toFixed(1)},${y(yMin).toFixed(1)}L${PADL},${y(yMin).toFixed(1)}Z`
  const priceAt = (p) => coin.open * (1 + p / 100)
  const ticks = [hi, (hi + lo) / 2, lo]
  const xticks = [0, Math.floor(path.length / 4), Math.floor(path.length / 2), Math.floor((3 * path.length) / 4), path.length - 1]

  const move = (e) => {
    const r = e.currentTarget.getBoundingClientRect()
    const cx = ((e.clientX ?? e.touches?.[0]?.clientX) - r.left) * (W / r.width)
    const i = Math.max(0, Math.min(path.length - 1, Math.round(((cx - PADL) / (W - PADL - PADR)) * (path.length - 1))))
    setHover(i)
  }
  const h = hover == null ? null : hover

  return (
    <>
      <svg className="ds-chart" viewBox={`0 0 ${W} ${H}`} onMouseMove={move} onMouseLeave={() => setHover(null)} onTouchMove={move} role="img"
        aria-label={`${coin.symbol} price through ${day}, ${up ? 'up' : 'down'} ${coin.move.toFixed(1)} percent`}>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={PADL} y1={y(t)} x2={W - PADR} y2={y(t)} stroke="var(--line)" strokeWidth="1" />
            <text x={PADL - 7} y={y(t) + 3.5} textAnchor="end" fontSize="10" fill="var(--mut)">${priceAt(t) < 1 ? priceAt(t).toFixed(5) : priceAt(t).toFixed(2)}</text>
          </g>
        ))}
        {path.some((v) => v <= 0) && path.some((v) => v >= 0) && (
          <line x1={PADL} y1={y(0)} x2={W - PADR} y2={y(0)} stroke="var(--mut)" strokeWidth="1" strokeDasharray="3 3" opacity="0.6" />
        )}
        <path d={area} fill={col} opacity="0.12" />
        <path d={d} fill="none" stroke={col} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        {vols.map((v, i) => (
          <rect key={i} x={x(i) - Math.max(0.7, (W - PADL - PADR) / path.length / 2.4)} y={base - vy(v)}
            width={Math.max(1.4, (W - PADL - PADR) / path.length / 1.2)} height={Math.max(0.6, vy(v))}
            fill="var(--mut)" opacity={h === i ? 0.85 : 0.32} />
        ))}
        <line x1={PADL} y1={base} x2={W - PADR} y2={base} stroke="var(--line)" strokeWidth="1" />
        {xticks.map((i, k) => (
          <text key={k} x={x(i)} y={H - 5} textAnchor={k === 0 ? 'start' : k === xticks.length - 1 ? 'end' : 'middle'}
            fontSize="10" fill="var(--mut)">{times[i]}</text>
        ))}
        {h != null && (
          <g>
            <line x1={x(h)} y1={PADT} x2={x(h)} y2={base} stroke="var(--fg)" strokeWidth="1" opacity="0.35" />
            <circle cx={x(h)} cy={y(path[h])} r="4" fill={col} stroke="var(--panel)" strokeWidth="2" />
          </g>
        )}
        <circle cx={x(path.length - 1)} cy={y(path[path.length - 1])} r="3.5" fill={col} />
      </svg>
      <div className="ds-readout">
        {h != null ? (
          <>
            <span>{times[h]}</span>
            <span><b>${priceAt(path[h]) < 1 ? priceAt(path[h]).toFixed(5) : priceAt(path[h]).toFixed(4)}</b></span>
            <span className={path[h] >= 0 ? 'pos' : 'neg'}><b>{pct(path[h])}</b> from open</span>
            <span>volume {vols[h] >= 1000 ? `${(vols[h] / 1000).toFixed(1)}k` : vols[h].toFixed(1)}</span>
          </>
        ) : <span>hover the chart to read any minute · bars below are volume</span>}
      </div>
    </>
  )
}

function CoinModal({ coin, day, onClose }) {
  React.useEffect(() => {
    const k = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  if (!coin) return null
  return (
    <div className="ds-modal" role="dialog" aria-modal="true" aria-label={`${coin.symbol} chart`}>
      <div className="ds-modal-scrim" onClick={onClose} />
      <div className="ds-modal-body">
        <div className="ds-modal-head">
         <div className="ds-modal-head-main">
          <div>
            <h2>{coin.symbol}</h2>
            <p className="ds-modal-sub">{day} · Austin time · {coin.path?.length || 0} points</p>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div className="ds-modal-price">{price(coin.price)}</div>
            <div className={coin.move >= 0 ? 'pos' : 'neg'} style={{ fontWeight: 600 }}>{pct(coin.move)} on the day</div>
          </div>
         </div>
          <button className="ds-x" onClick={onClose} aria-label="Close chart">×</button>
        </div>
        <div className="ds-modal-stats">
          <div><span>open</span><b>{price(coin.open)}</b></div>
          <div><span>high</span><b>{price(coin.high)}</b></div>
          <div><span>low</span><b>{price(coin.low)}</b></div>
          <div><span>swing</span><b>{coin.swing?.toFixed(2)}%</b></div>
          <div><span>open→high</span><b>{pct(coin.open_high, 1)}</b></div>
          <div><span>off high</span><b>{pct(coin.off_high, 1)}</b></div>
          <div><span>vol vs 7d</span><b className={(coin.vol_vs_7d || 0) >= 1.5 ? 'hot' : ''}>{mult(coin.vol_vs_7d)}</b></div>
          <div><span>vol peak</span><b>{coin.vol_peak_hour || '—'}</b></div>
          <div><span>RSI</span><b>{coin.rsi != null ? coin.rsi.toFixed(0) : '—'}</b></div>
        </div>
        <CoinChart coin={coin} day={day} />
      </div>
    </div>
  )
}

export default function DayScan() {
  const [day, setDay] = React.useState(todayISO())
  const [asOf, setAsOf] = React.useState('')          // '' = whole day
  const [open, setOpen] = React.useState(false)
  const [onlyPlay, setOnlyPlay] = React.useState(false)
  const [statusFilter, setStatusFilter] = React.useState(F_ALL)   // default: hide nothing
  const [sort, setSort] = React.useState({ k: 'move', dir: -1 })
  const [rangeEnd, setRangeEnd] = React.useState(todayISO())
  const [rangeDays, setRangeDays] = React.useState(14)
  const [hotVol, setHotVol] = React.useState(1.5)
  const [pick, setPick] = React.useState(null)

  const live = day === todayISO() && asOf === ''
  const s = useAsync(
    // Show EVERY coin and let the table sort. Hiding two thirds of the board
    // by default is how the desk came to be judged on 33 of 75 coins.
    () => api.dayScan(day, { tradableOnly: false, asOfHour: asOf === '' ? undefined : Number(asOf), hotVolume: hotVol }),
    [day, asOf, hotVol], live ? 60000 : undefined,
  )
  const rangeStart = shift(rangeEnd, -(rangeDays - 1))
  const r = useAsync(() => (open ? api.dayScanRange(rangeStart, rangeEnd, true, hotVol) : Promise.resolve(null)),
    [open, rangeStart, rangeEnd, hotVol])

  const shown = React.useRef(null)
  if (s.data) shown.current = s.data
  const view = s.data || shown.current

  const cnt = view?.counts || null

  // Rows for the day before the status filter. `only in play` is applied first so
  // every status count below is exactly what clicking that button will show.
  const base = React.useMemo(
    () => (view?.coins || []).filter((c) => !onlyPlay || c.in_play),
    [view, onlyPlay],
  )

  // Filter options built from the rows already loaded -- no refetch, so switching
  // is instant and can never change which coins were scanned. Note that
  // "tradable on Robinhood" spans two status values (it is rh_confirmed, the
  // venue's answer) while "tradeable" is our own role gate. Both stay visible on
  // purpose: treating them as one number is what made the count look unstable.
  const filters = React.useMemo(() => {
    const seen = new Map()
    for (const c of base) seen.set(c.status, (seen.get(c.status) || 0) + 1)
    const rank = (k) => (STATUS_ORDER.indexOf(k) < 0 ? 99 : STATUS_ORDER.indexOf(k))
    const ordered = [...seen.keys()].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
    return [
      { k: F_ALL, label: 'all', n: base.length },
      { k: F_RH, label: 'tradable on Robinhood', n: base.filter((c) => c.rh_tradable).length },
      ...ordered.map((st) => ({ k: `s:${st}`, label: st, n: seen.get(st) })),
    ]
  }, [base])

  // A status with no rows today falls back to all, so changing the date can never
  // leave an empty table with nothing on screen explaining why.
  const active = filters.some((f) => f.k === statusFilter) ? statusFilter : F_ALL

  const filtered = React.useMemo(() => base.filter((c) => (
    active === F_ALL ? true : active === F_RH ? !!c.rh_tradable : c.status === active.slice(2)
  )), [base, active])

  const coins = React.useMemo(() => {
    const get = (c) => (sort.k === 'symbol' ? c.symbol : c[sort.k])
    return [...filtered].sort((a, a2) => {
      const x = get(a); const y = get(a2)
      if (typeof x === 'string') return sort.dir * x.localeCompare(y)
      return sort.dir * ((x ?? -Infinity) - (y ?? -Infinity))
    })
  }, [filtered, sort])

  const th = (k, label, tip) => (
    <th key={k} className={k === 'symbol' ? '' : 'num'} onClick={() => setSort((p) => ({ k, dir: p.k === k ? -p.dir : (k === 'symbol' ? 1 : -1) }))} style={{ cursor: 'pointer' }}>
      {label}{tip ? <Info text={tip} /> : null}{sort.k === k ? (sort.dir < 0 ? ' ▼' : ' ▲') : ''}
    </th>
  )

  const play = filtered.filter((c) => c.in_play).sort((a, b) => b.move - a.move)

  return (
    <div className={`ds-shell${open ? ' ds-open' : ''}`}>
      <aside className="ds-drawer" aria-hidden={!open}>
        <div className="ds-drawer-head">
          <strong>Days</strong>
          <button className="ds-x" onClick={() => setOpen(false)} aria-label="Close day list">×</button>
        </div>
        <div className="ds-range">
          <label>ending<input type="date" value={rangeEnd} max={todayISO()} onChange={(e) => setRangeEnd(e.target.value)} /></label>
          <label>span
            <select value={rangeDays} onChange={(e) => setRangeDays(Number(e.target.value))}>
              <option value={7}>7 days</option><option value={14}>14 days</option>
              <option value={30}>30 days</option><option value={60}>60 days</option>
            </select>
          </label>
        </div>
        <div className="ds-days">
          {r.loading && <div className="mut" style={{ padding: '10px 12px' }}>loading…</div>}
          {r.error && <div className="neg" style={{ padding: '10px 12px' }}>{String(r.error.message || r.error)}</div>}
          {(r.data?.days || []).map((d) => (
            <button key={d.date} className={`ds-day${d.date === day ? ' on' : ''}`} onClick={() => { setDay(d.date); setOpen(false) }}>
              <span className="ds-day-date">{d.date}</span>
              <span className={`ds-chip${d.in_play ? ' hot' : ''}`}>{d.in_play} in play</span>
              <span className="ds-day-movers">{d.movers.length ? d.movers.join(' · ') : <span className="mut">quiet</span>}</span>
              <span className="ds-day-best mut">
                best {d.best_symbol || '—'} {d.best_open_high != null ? pct(d.best_open_high, 1) : ''}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <div className="ds-main">
        <div className="ds-topline">
          <button className="ds-burger" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            <span /><span /><span /> Days
          </button>
          <div className="ds-nav">
            <button onClick={() => setDay(shift(day, -1))} aria-label="Previous day">‹</button>
            <input type="date" value={day} max={todayISO()} onChange={(e) => setDay(e.target.value)} />
            <button onClick={() => setDay(shift(day, 1))} disabled={day >= todayISO()} aria-label="Next day">›</button>
            <button className="ds-today" onClick={() => { setDay(todayISO()); setAsOf('') }} disabled={live}>today</button>
          </div>
          <label className="ds-asof">
            replay as of
            <select value={asOf} onChange={(e) => setAsOf(e.target.value)}>
              <option value="">whole day</option>
              {Array.from({ length: 23 }, (_, i) => i + 1).map((h) => (
                <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>
              ))}
            </select>
            <Info text="Rebuilds the table using only bars up to that hour, so a past day looks exactly as it did at the time. This is how you check whether the tell was there before the move — nothing after the chosen hour is used." />
          </label>
          <label className="ds-asof">
            in play at
            <select value={hotVol} onChange={(e) => setHotVol(Number(e.target.value))}>
              <option value={1.5}>1.5× volume — ~9/day, catches the day's best 44%</option>
              <option value={2}>2× volume — ~5/day, catches 33%</option>
              <option value={3}>3× volume — ~3/day, catches 21%, most room each</option>
            </select>
            <Info text="Swept over June 2024 to Sep 2026, 823 days. A wider net flags the day's biggest mover more often but each pick carries less upside; a tighter net is the reverse. Percentages are how often the day's best coin was among the flagged ones at 04:00." />
          </label>
          <label className="ds-check">
            <input type="checkbox" checked={onlyPlay} onChange={(e) => setOnlyPlay(e.target.checked)} /> only in play
          </label>
          {s.loading && <span className="ds-busy">loading {day}…</span>}
        </div>

        <h1>Day Scan</h1>
        {cnt && (
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: '4px 9px', margin: '0 0 6px' }}>
            <b style={{ fontSize: 16 }}>{cnt.scanned} coins scanned</b>
            <span className="mut">·</span>
            <b className="pos" style={{ fontSize: 16 }}>{cnt.rh_tradable} tradable on Robinhood</b>
            <span className="mut">·</span>
            <b className="mut" style={{ fontSize: 16 }}>{cnt.not_rh_tradable} not tradable</b>
            <Info text={`Three counts, one row each, and they add up: ${cnt.rh_tradable} + ${cnt.not_rh_tradable} = ${cnt.scanned}. "Tradable on Robinhood" means universe.rh_confirmed — Robinhood listed the pair as USD and is_api_tradable, so it will take the order. That is NOT the same as our own role gate: of those ${cnt.rh_tradable}, only ${cnt.engine_enabled} carry role core or tradeable, which is what the engine is allowed to trade today. ${cnt.universe_active} active coins are tracked in total${cnt.no_bars_today ? `, and ${cnt.no_bars_today} of them have no bars for this day so they are not in the table` : ' and every one of them has bars for this day'}.`} />
          </div>
        )}
        <p className="sub">
          Every active coin we track, for one day, Austin time.
          {cnt ? ` Of the ${cnt.rh_tradable} Robinhood will fill, ${cnt.engine_enabled} are enabled for the engine now (universe role core or tradeable) and ${cnt.rh_not_enabled} are watch-only.` : ''}
          {view ? ` ${view.up} up · ${view.in_play} in play · bars every ${view.granularity_s / 60}m` : ''}
          <Info text="In play = volume running at least 2x this coin's own normal AND swinging at least 1.5x wider than normal. Both at once. Across 2022-2026 those days closed up 54-64% of the time versus 45% for quiet days." />
        </p>

        {asOf !== '' && (
          <Banner kind="info" title={`Replaying ${day} as it looked at ${String(asOf).padStart(2, '0')}:00`}>
            Nothing after {String(asOf).padStart(2, '0')}:00 is used. Buying in the 01:00–08:00 window on a coin
            flagged here left an average of ~5% still available before the day's high, and reached +3% on about
            half of those days. The exit is the unsolved part — holding to the close gives most of it back.
          </Banner>
        )}
        {s.error && <Banner kind="warn" title="Could not load this day">{String(s.error.message || s.error)}</Banner>}

        {play.length > 0 && (
          <div className="ds-cards">
            {play.map((c) => (
              <div className="ds-card" key={c.symbol} onClick={() => setPick(c.symbol)} role="button" tabIndex={0}
                onKeyDown={(e) => { if (e.key === 'Enter') setPick(c.symbol) }}>
                <div className="ds-card-top">
                  <strong>{c.symbol}</strong>
                  <span className={c.move >= 0 ? 'pos' : 'neg'} style={{ fontSize: 18, fontWeight: 600 }}>{pct(c.move)}</span>
                </div>
                <Spark path={c.path} up={c.move >= 0} w={280} h={64} />
                <div className="ds-card-facts">
                  <div><span>swing</span><b>{pct(c.swing, 1).replace('+', '')}</b></div>
                  <div><span>open→high</span><b>{pct(c.open_high, 1)}</b></div>
                  <div><span>off high</span><b>{pct(c.off_high, 1)}</b></div>
                  <div><span>vol vs 7d</span><b className="hot">{mult(c.vol_vs_7d)}</b></div>
                  <div><span>vol peak</span><b>{c.vol_peak_hour || '—'}</b></div>
                  <div><span>swing peak</span><b>{c.swing_peak_hour || '—'}</b></div>
                </div>
              </div>
            ))}
          </div>
        )}

        <Card title={`${day}${asOf !== '' ? ` as of ${String(asOf).padStart(2, '0')}:00` : ''} · ${coins.length === base.length ? `${coins.length} coins` : `${coins.length} of ${base.length} coins`}`}>
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, margin: '0 0 10px' }}>
            <span className="mut" style={{ fontSize: 12 }}>status</span>
            {filters.map((f) => (
              <button key={f.k} onClick={() => setStatusFilter(f.k)} aria-pressed={active === f.k}
                style={{
                  padding: '3px 10px', fontSize: 12, borderRadius: 999,
                  background: active === f.k ? 'var(--accent-strong)' : 'var(--surface)',
                  borderColor: active === f.k ? 'var(--accent)' : 'var(--border)',
                  fontWeight: active === f.k ? 600 : 400,
                }}>
                {f.label} <span style={{ opacity: 0.65 }}>{f.n}</span>
              </button>
            ))}
            <Info text={'Filters the rows already loaded, so it is instant and nothing is refetched — the scan itself always covers every active coin. "tradable on Robinhood" is the union of "tradeable" and "on RH, not enabled", because both are pairs Robinhood will fill; they differ only in whether our own role gate has enabled them. The status column stays sortable either way.'} />
          </div>
          <div className="ds-tblwrap">
            <table className="tbl ds-tbl">
              <thead>
                <tr>
                  {th('symbol', 'coin')}
                  {th('status', 'status', 'tradeable = the engine can trade it now. "on RH, not enabled" = Robinhood allows it but a gate (cost or history) holds it back. Sort by this column to see only what is reachable.')}
                  <th>today</th>
                  {th('price', 'price', 'Last traded price we have for this day.')}
                  {th('move', 'move', 'Day open → last price. Not the high.')}
                  {th('swing', 'swing', "Day's low → day's high. The total distance travelled.")}
                  {th('open_high', 'open→high', "Day's open → day's high. The best exit that existed.")}
                  {th('off_high', 'off high', "How far below the day's high it ended up.")}
                  {th('volume_usd', 'volume $', 'Dollars traded, volume × price.')}
                  {th('vol_vs_7d', 'vol vs 7d', "This day's volume pace against this coin's own average day over the prior week.")}
                  {th('vol_vs_30d', 'vol vs 30d', 'Same, against the prior 30 days.')}
                  {th('vol_peak_share', 'vol peak', 'The clock hour holding the most volume, and its share of the day.')}
                  {th('swing_peak_pct', 'swing peak', 'The clock hour with the widest high-to-low, and how wide it was.')}
                  {th('rsi', 'RSI', 'RSI(14) on hourly closes entering the day. Under 30 oversold.')}
                </tr>
              </thead>
              <tbody>
                {!s.loading && coins.length === 0 && (
                  <tr><td colSpan={14} className="mut">
                    {base.length === 0 ? 'no coins for this day'
                      : `no coins with this status today — ${base.length} scanned, pick "all" above`}
                  </td></tr>
                )}
                {coins.map((c) => (
                  <tr key={c.symbol} className={c.in_play ? 'ds-hit' : ''} onClick={() => setPick(c.symbol)} title="open chart">
                    <td><strong>{c.symbol}</strong></td>
                    <td className={c.tradable ? 'pos' : 'mut'} style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{c.status}</td>
                    <td><Spark path={c.path} up={c.move >= 0} /></td>
                    <td className="num">{price(c.price)}</td>
                    <td className={`num ${c.move >= 0 ? 'pos' : 'neg'}`}>{pct(c.move)}</td>
                    <td className="num">{c.swing?.toFixed(2)}%</td>
                    <td className="num">{pct(c.open_high)}</td>
                    <td className="num mut">{pct(c.off_high, 1)}</td>
                    <td className="num">{c.volume_usd ? `$${(c.volume_usd / 1e6).toFixed(1)}M` : '—'}</td>
                    <td className={`num ${(c.vol_vs_7d || 0) >= 2 ? 'hot' : ''}`}>{mult(c.vol_vs_7d)}</td>
                    <td className="num">{mult(c.vol_vs_30d)}</td>
                    <td className="num">{c.vol_peak_hour || '—'} <span className="mut">{c.vol_peak_share != null ? `${c.vol_peak_share.toFixed(0)}%` : ''}</span></td>
                    <td className="num">{c.swing_peak_hour || '—'} <span className="mut">{c.swing_peak_pct != null ? `${c.swing_peak_pct.toFixed(1)}%` : ''}</span></td>
                    <td className="num">{c.rsi != null ? c.rsi.toFixed(0) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      {open && <div className="ds-scrim" onClick={() => setOpen(false)} />}
      <CoinModal coin={(view?.coins || []).find((c) => c.symbol === pick)} day={view?.date || day} onClose={() => setPick(null)} />
    </div>
  )
}
