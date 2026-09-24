import React from 'react'
import { api } from '../lib/api.js'

const fmtPx = (v) => (v == null ? '—' : `$${v < 1 ? v.toFixed(5) : v < 100 ? v.toFixed(4) : v.toFixed(2)}`)
const fmtVol = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : v.toFixed(1))

/** One coin, one day. Line or candles, volume underneath, hover to read any bar. */
export default function CoinChart({ symbol, day, entry, exit, onClose }) {
  // Line by default. He asked to "see the graph line of live prices"; candles
  // answer a different question and are one click away.
  const [style, setStyle] = React.useState('line')
  const [gran, setGran] = React.useState(3600)
  const [data, setData] = React.useState(null)
  const [err, setErr] = React.useState(null)
  const [hover, setHover] = React.useState(null)

  React.useEffect(() => {
    let alive = true
    setData(null); setErr(null)
    api.chartFor(symbol, day, gran)
      .then((d) => { if (alive) setData(d) })
      .catch((e) => { if (alive) setErr(String(e.message || e)) })
    return () => { alive = false }
  }, [symbol, day, gran])

  React.useEffect(() => {
    const k = (e) => { if (e.key === 'Escape' && onClose) onClose() }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])

  const bars = data?.bars || []
  const W = 880, H = 300, VH = 62, PADL = 62, PADR = 14, PADT = 14, PADB = 22
  const plotH = H - PADT - PADB - VH
  const lo = bars.length ? Math.min(...bars.map((b) => b.l ?? b.c)) : 0
  const hi = bars.length ? Math.max(...bars.map((b) => b.h ?? b.c)) : 1
  const pad = (hi - lo) * 0.06 || 1
  const yMin = lo - pad, yMax = hi + pad
  const x = (i) => PADL + (i * (W - PADL - PADR)) / Math.max(1, bars.length - 1)
  const y = (v) => PADT + (1 - (v - yMin) / (yMax - yMin)) * plotH
  const vMax = Math.max(...bars.map((b) => b.v || 0), 1e-9)
  const base = H - PADB
  const bw = Math.max(1.6, (W - PADL - PADR) / Math.max(1, bars.length) * 0.62)

  const first = bars[0]?.o ?? bars[0]?.c
  const last = bars[bars.length - 1]?.c
  const chg = first && last ? (last / first - 1) * 100 : null
  const up = (chg ?? 0) >= 0
  const line = 'M' + bars.map((b, i) => `${x(i).toFixed(1)},${y(b.c).toFixed(1)}`).join('L')

  const move = (e) => {
    if (!bars.length) return
    const r = e.currentTarget.getBoundingClientRect()
    const cx = ((e.clientX ?? e.touches?.[0]?.clientX) - r.left) * (W / r.width)
    setHover(Math.max(0, Math.min(bars.length - 1,
      Math.round(((cx - PADL) / (W - PADL - PADR)) * (bars.length - 1)))))
  }
  const hb = hover != null ? bars[hover] : null
  const ticks = [yMax - pad, (yMax + yMin) / 2, yMin + pad]
  const xt = bars.length ? [0, Math.floor(bars.length / 3), Math.floor(2 * bars.length / 3), bars.length - 1] : []

  return (
    <div className="ds-modal" role="dialog" aria-modal="true" aria-label={`${symbol} chart`}>
      <div className="ds-modal-scrim" onClick={onClose} />
      <div className="ds-modal-body">
        <div className="ds-modal-head">
         <div className="ds-modal-head-main">
          <div>
            <h2>{symbol}</h2>
            <p className="ds-modal-sub">
              {day || 'most recent'} · Austin time
              {data?.spread_pct != null && <> · spread {data.spread_pct.toFixed(2)}%/side</>}
            </p>
          </div>
          {/* The big number follows your finger. Reading a price meant looking
              at the small line under the chart while your hand was on the chart
              itself; the number you are pointing at belongs where you are
              already looking. It falls back to the live price the moment you
              let go. */}
          <div style={{ textAlign: 'right' }}>
            <div className="ds-modal-price">{fmtPx(hb ? hb.c : (data?.live_px ?? last))}</div>
            {hb ? (
              <div className="mut" style={{ fontWeight: 600, fontSize: 12 }}>
                at {hb.t}
                {last ? (
                  <span className={hb.c >= last ? 'pos' : 'neg'}>
                    {'  '}{hb.c >= last ? '+' : ''}
                    {(((hb.c / last) - 1) * 100).toFixed(2)}% vs now
                  </span>
                ) : null}
              </div>
            ) : chg != null ? (
              <div className={up ? 'pos' : 'neg'} style={{ fontWeight: 600 }}>
                {chg >= 0 ? '+' : ''}{chg.toFixed(2)}% on the day
                {data?.live_px != null && (
                  data.live_stale
                    ? <span className="mut"> · {Math.round((data.live_age_s || 0) / 60)}m old</span>
                    : <span className="mut"> · live</span>
                )}
              </div>
            ) : null}
          </div>
         </div>
          <button className="ds-x" onClick={onClose} aria-label="Close chart">×</button>
        </div>

        <div className="row" style={{ gap: 8, margin: '10px 0 4px', flexWrap: 'wrap' }}>
          <div className="seg">
            <button className={style === 'candle' ? 'on' : ''} onClick={() => setStyle('candle')}>candles</button>
            <button className={style === 'line' ? 'on' : ''} onClick={() => setStyle('line')}>line</button>
          </div>
          <div className="seg">
            {[[900, '15m'], [3600, '1h']].map(([g, l]) => (
              <button key={g} className={gran === g ? 'on' : ''} onClick={() => setGran(g)}>{l}</button>
            ))}
          </div>
          <div className="spacer" />
          <span className="mut" style={{ fontSize: 12 }}>{bars.length} bars</span>
        </div>

        {err && <div className="err">{err}</div>}
        {!data && !err && <p className="mut">loading…</p>}
        {data && !bars.length && <p className="mut">no bars stored for {symbol} on this day</p>}

        {bars.length > 0 && (
          <>
            <svg className="ds-chart" viewBox={`0 0 ${W} ${H}`} onMouseMove={move}
                 onMouseLeave={() => setHover(null)} onTouchMove={move}
                 onTouchEnd={() => setHover(null)}
                 style={{ touchAction: 'none' }} role="img"
                 aria-label={`${symbol} ${style} chart, ${up ? 'up' : 'down'} ${(chg ?? 0).toFixed(1)} percent`}>
              {ticks.map((t, i) => (
                <g key={i}>
                  <line x1={PADL} y1={y(t)} x2={W - PADR} y2={y(t)} stroke="var(--line)" strokeWidth="1" />
                  <text x={PADL - 8} y={y(t) + 3.5} textAnchor="end" fontSize="10" fill="var(--mut)">{fmtPx(t)}</text>
                </g>
              ))}
              {entry != null && y(entry) > PADT && y(entry) < base && (
                <g>
                  <line x1={PADL} y1={y(entry)} x2={W - PADR} y2={y(entry)} stroke="var(--accent)"
                        strokeWidth="1.5" strokeDasharray="5 3" />
                  <text x={W - PADR - 2} y={y(entry) - 4} textAnchor="end" fontSize="10" fill="var(--accent)">
                    entry {fmtPx(entry)}
                  </text>
                </g>
              )}
              {exit != null && y(exit) > PADT && y(exit) < base && (
                <g>
                  <line x1={PADL} y1={y(exit)} x2={W - PADR} y2={y(exit)} stroke="var(--mut)"
                        strokeWidth="1.5" strokeDasharray="2 3" />
                  <text x={W - PADR - 2} y={y(exit) - 4} textAnchor="end" fontSize="10" fill="var(--mut)">
                    exit {fmtPx(exit)}
                  </text>
                </g>
              )}

              {style === 'line' ? (
                <>
                  <path d={`${line}L${x(bars.length - 1).toFixed(1)},${y(yMin).toFixed(1)}L${PADL},${y(yMin).toFixed(1)}Z`}
                        fill={up ? 'var(--pos)' : 'var(--neg)'} opacity="0.11" />
                  <path d={line} fill="none" stroke={up ? 'var(--pos)' : 'var(--neg)'} strokeWidth="2.5"
                        strokeLinejoin="round" strokeLinecap="round" />
                </>
              ) : (
                bars.map((b, i) => {
                  const g = (b.c ?? 0) >= (b.o ?? b.c)
                  const col = g ? 'var(--pos)' : 'var(--neg)'
                  const yo = y(b.o ?? b.c), yc = y(b.c)
                  return (
                    <g key={i} opacity={hover == null || hover === i ? 1 : 0.72}>
                      <line x1={x(i)} y1={y(b.h ?? b.c)} x2={x(i)} y2={y(b.l ?? b.c)} stroke={col} strokeWidth="1" />
                      <rect x={x(i) - bw / 2} y={Math.min(yo, yc)} width={bw}
                            height={Math.max(1, Math.abs(yc - yo))} fill={col} />
                    </g>
                  )
                })
              )}

              {bars.map((b, i) => (
                <rect key={`v${i}`} x={x(i) - bw / 2} y={base - ((b.v || 0) / vMax) * (VH - 10)}
                      width={bw} height={Math.max(0.6, ((b.v || 0) / vMax) * (VH - 10))}
                      fill="var(--mut)" opacity={hover === i ? 0.85 : 0.3} />
              ))}
              <line x1={PADL} y1={base} x2={W - PADR} y2={base} stroke="var(--line)" strokeWidth="1" />
              {xt.map((i, k) => (
                <text key={k} x={x(i)} y={H - 6} fontSize="10" fill="var(--mut)"
                      textAnchor={k === 0 ? 'start' : k === xt.length - 1 ? 'end' : 'middle'}>{bars[i].t}</text>
              ))}
              {hover != null && (
                <line x1={x(hover)} y1={PADT} x2={x(hover)} y2={base} stroke="var(--fg)" strokeWidth="1" opacity="0.32" />
              )}
            </svg>

            <div className="ds-readout">
              {hb ? (
                <>
                  <span>{hb.t}</span>
                  <span>O <b>{fmtPx(hb.o)}</b></span>
                  <span>H <b>{fmtPx(hb.h)}</b></span>
                  <span>L <b>{fmtPx(hb.l)}</b></span>
                  <span>C <b>{fmtPx(hb.c)}</b></span>
                  <span>vol <b>{fmtVol(hb.v || 0)}</b></span>
                </>
              ) : <span>hover any bar to read it · grey bars underneath are volume</span>}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
