import React, { useMemo, useRef, useState } from 'react'

/**
 * Chart primitives.
 *
 * Palette: the validated dark categorical slots (blue, orange, aqua, yellow,
 * magenta, violet), checked against this app's chart surface #1E293B — all six
 * pass the lightness band, chroma floor, CVD separation, normal-vision floor and
 * 3:1 contrast. Slots are assigned to strategies in FIXED order and never cycled,
 * so a strategy keeps its colour when the filter changes the series count.
 *
 * Sign (profit vs loss) is polarity, so it uses the validated diverging pair
 * blue<->red with a neutral zero line — not green/red, which is the classic
 * colour-blind failure. Every bar also carries its value on hover and in the
 * table view, so sign is never carried by colour alone.
 */
export const SERIES = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#9085e9']
export const POS = '#3987e5'
export const NEG = '#e66767'
export const AXIS = '#475569'
export const GRID = '#293548'
export const INK = '#F1F5F9'
export const INK_MUTED = '#94A3B8'

const fmtUsd = (v) => (v == null ? '—' : `${v < 0 ? '-' : ''}$${Math.abs(v).toFixed(2)}`)

function useSize(ref, fallback = 720) {
  const [w, setW] = useState(fallback)
  React.useEffect(() => {
    if (!ref.current) return
    const ro = new ResizeObserver((es) => setW(es[0].contentRect.width))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [ref])
  return w
}

/** Multi-series line chart with a crosshair tooltip and end labels. */
export function LineChart({ series, height = 260, yLabel = '', valueFormat = fmtUsd, xFormat }) {
  const wrap = useRef(null)
  const width = useSize(wrap)
  const [hover, setHover] = useState(null)
  const [showTable, setShowTable] = useState(false)

  const m = { top: 14, right: 92, bottom: 26, left: 56 }
  const iw = Math.max(80, width - m.left - m.right)
  const ih = height - m.top - m.bottom

  const { xs, xMin, xMax, yMin, yMax, paths } = useMemo(() => {
    const all = series.flatMap((s) => s.points)
    if (all.length === 0) return { xs: [], xMin: 0, xMax: 1, yMin: 0, yMax: 1, paths: [] }
    const xMin = Math.min(...all.map((p) => p.x))
    const xMax = Math.max(...all.map((p) => p.x))
    let yMin = Math.min(...all.map((p) => p.y))
    let yMax = Math.max(...all.map((p) => p.y))
    if (yMin === yMax) { yMin -= 1; yMax += 1 }
    const pad = (yMax - yMin) * 0.12
    yMin -= pad; yMax += pad
    const sx = (x) => ((x - xMin) / (xMax - xMin || 1)) * iw
    const sy = (y) => ih - ((y - yMin) / (yMax - yMin || 1)) * ih
    const paths = series.map((s) => ({
      ...s,
      d: s.points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' '),
      last: s.points[s.points.length - 1],
      sx, sy,
    }))
    return { xs: all.map((p) => p.x), xMin, xMax, yMin, yMax, paths }
  }, [series, iw, ih])

  const sx = (x) => ((x - xMin) / (xMax - xMin || 1)) * iw
  const sy = (y) => ih - ((y - yMin) / (yMax - yMin || 1)) * ih
  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => yMin + ((yMax - yMin) * i) / ticks)

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const px = e.clientX - rect.left - m.left
    if (px < 0 || px > iw) return setHover(null)
    const xv = xMin + (px / iw) * (xMax - xMin)
    const at = series.map((s) => {
      let best = null
      for (const p of s.points) {
        if (best === null || Math.abs(p.x - xv) < Math.abs(best.x - xv)) best = p
      }
      return { name: s.name, color: s.color, point: best }
    }).filter((a) => a.point)
    setHover({ px, xv, at })
  }

  if (series.length === 0 || series.every((s) => s.points.length === 0)) {
    return <div className="mut">no data in this range</div>
  }

  return (
    <div ref={wrap} style={{ position: 'relative' }}>
      <div className="row" style={{ marginBottom: 6 }}>
        {series.map((s) => (
          <span key={s.name} className="legend-item">
            <span className="legend-swatch" style={{ background: s.color }} />
            {s.name}
          </span>
        ))}
        <div className="spacer" />
        <button className="why" onClick={() => setShowTable((v) => !v)}>
          {showTable ? 'hide table' : 'table view'}
        </button>
      </div>

      <svg width={width} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)}
           role="img" aria-label={yLabel || 'time series'}>
        <g transform={`translate(${m.left},${m.top})`}>
          {yTicks.map((t, i) => (
            <g key={i}>
              <line x1={0} x2={iw} y1={sy(t)} y2={sy(t)} stroke={GRID} strokeWidth={1} />
              <text x={-8} y={sy(t)} dy="0.32em" textAnchor="end" fontSize={11} fill={INK_MUTED}>
                {valueFormat(t)}
              </text>
            </g>
          ))}
          <line x1={0} x2={iw} y1={sy(Math.max(yMin, Math.min(yMax, 0)))}
                y2={sy(Math.max(yMin, Math.min(yMax, 0)))} stroke={AXIS} strokeWidth={1.5} />

          {paths.map((p) => (
            <path key={p.name} d={p.d} fill="none" stroke={p.color} strokeWidth={2}
                  strokeLinejoin="round" strokeLinecap="round" />
          ))}

          {paths.map((p) => p.last && (
            <text key={`l-${p.name}`} x={iw + 8} y={sy(p.last.y)} dy="0.32em"
                  fontSize={11} fill={INK}>{valueFormat(p.last.y)}</text>
          ))}

          {hover && (
            <>
              <line x1={hover.px} x2={hover.px} y1={0} y2={ih} stroke={AXIS} strokeWidth={1} strokeDasharray="3 3" />
              {hover.at.map((a) => (
                <circle key={a.name} cx={sx(a.point.x)} cy={sy(a.point.y)} r={5}
                        fill={a.color} stroke="#1E293B" strokeWidth={2} />
              ))}
            </>
          )}

          <text x={0} y={ih + 18} fontSize={11} fill={INK_MUTED}>
            {xFormat ? xFormat(xMin) : ''}
          </text>
          <text x={iw} y={ih + 18} fontSize={11} fill={INK_MUTED} textAnchor="end">
            {xFormat ? xFormat(xMax) : ''}
          </text>
        </g>
      </svg>

      {hover && (
        <div className="tooltip" style={{ left: Math.min(hover.px + m.left + 12, width - 190) }}>
          <div className="mut" style={{ fontSize: 11 }}>{xFormat ? xFormat(hover.xv) : ''}</div>
          {hover.at.map((a) => (
            <div key={a.name} className="row" style={{ gap: 6 }}>
              <span className="legend-swatch" style={{ background: a.color }} />
              <span>{a.name}</span><div className="spacer" />
              <span className="mono">{valueFormat(a.point.y)}</span>
            </div>
          ))}
        </div>
      )}

      {showTable && (
        <div className="scroll" style={{ maxHeight: 240, marginTop: 8 }}>
          <table>
            <thead><tr><th>x</th>{series.map((s) => <th key={s.name} className="num">{s.name}</th>)}</tr></thead>
            <tbody>
              {series[0].points.map((p, i) => (
                <tr key={i}>
                  <td>{xFormat ? xFormat(p.x) : p.x}</td>
                  {series.map((s) => <td key={s.name} className="num">{valueFormat(s.points[i]?.y)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** Bar chart with a zero baseline; sign encoded by the diverging pair AND by the label. */
/* ── DayBars ──────────────────────────────────────────────────────────────
 *
 * One measure per day, readable with a finger. Replaces a bare SVG whose only
 * explanation was a native <title> -- which needs a mouse held still for a
 * second, and on a phone does not exist at all, so the bars were decoration.
 *
 * Deliberately ONE series per view. "Net per day" and "running total" differ by
 * an order of magnitude over a month, and drawing them together needs a second
 * y-axis, which makes any relationship between the two lines an artefact of
 * where you put the axes. They are views you switch between instead.
 *
 * `views`: [{ key, label, pick(row, i, rows), kind: 'bar'|'line', format, tone }]
 *
 * `tone: 'neutral'` exists because SIGN IS NOT ALWAYS MEANING. Spread cost is
 * money that left the account, so it belongs below the line -- but drawn in the
 * loss colour it reads as "you lost this", and on 2026-09-19 it did exactly
 * that: a day whose only losing trade was -$7.05 was read as "over -$14 of
 * losses", which was the day's total SPREAD.
 *
 * The neutral is INK_MUTED (#94A3B8) and it is GREY ON PURPOSE. A palette
 * validator will flag it as "reads gray / outside the lightness band", which is
 * the right call for a CATEGORICAL palette and the wrong one here: cost is not
 * a third category competing with profit and loss, it is a magnitude that must
 * NOT carry the emotional weight of either. What matters is that it is readable
 * (contrast 3:1+ against the surface, CVD dE 11.1 against the loss red) and that
 * only one series is ever drawn at a time. Do not "fix" this by giving it a
 * hue.
 * `detail`: (row) => [[label, value], …]  the readout under the chart.
 */
export function DayBars({ rows, views, height = 150, detail }) {
  const [vi, setVi] = React.useState(0)
  const [hi, setHi] = React.useState(null)
  const wrapRef = React.useRef(null)
  if (!rows?.length || !views?.length) return null
  const v = views[Math.min(vi, views.length - 1)]
  const vals = rows.map((r, i) => Number(v.pick(r, i, rows)) || 0)
  const fmtV = v.format || fmtUsd

  const W = 720, PADL = 46, PADR = 10, PADT = 16, PADB = 20
  const plotH = height - PADT - PADB
  const max = Math.max(...vals, 0)
  const min = Math.min(...vals, 0)
  const span = (max - min) || 1
  const y = (n) => PADT + (1 - (n - min) / span) * plotH
  const step = (W - PADL - PADR) / rows.length
  const bw = Math.max(2, Math.min(26, step * 0.62))
  const cx = (i) => PADL + step * i + step / 2
  const zero = y(0)

  // Pointer, not mouse: the same handler serves a trackpad and a thumb.
  const onMove = (e) => {
    const el = wrapRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const xPx = ((e.clientX - r.left) / r.width) * W
    const i = Math.floor((xPx - PADL) / step)
    setHi(i >= 0 && i < rows.length ? i : null)
  }
  const row = hi != null ? rows[hi] : null
  const pairs = row ? (detail ? detail(row, hi, rows) : [[v.label, fmtV(vals[hi])]]) : null

  return (
    <div>
      {views.length > 1 && (
        <div className="seg" style={{ marginBottom: 6 }}>
          {views.map((x, i) => (
            <button key={x.key} type="button" className={i === vi ? 'on' : ''}
                    onClick={() => setVi(i)}>{x.label}</button>
          ))}
        </div>
      )}
      {/* The readout sits ABOVE the chart and keeps its height whether or not
          anything is hovered, so the page does not jump as you move across. */}
      <div className="daybars-read">
        {row ? (
          <>
            <b>{row.day}</b>
            {pairs.map(([k, val, tone]) => (
              <span key={k}>{k} <b className={tone || ''}>{val}</b></span>
            ))}
          </>
        ) : (
          <span className="mut">{v.hint || 'point at a bar for that day’s numbers'}</span>
        )}
      </div>
      <div ref={wrapRef} className="daybars-wrap"
           onPointerMove={onMove} onPointerDown={onMove}
           onPointerLeave={() => setHi(null)}>
        <svg viewBox={`0 0 ${W} ${height}`} style={{ width: '100%', height }}
             preserveAspectRatio="none" role="img"
             aria-label={`${v.label} by day, ${rows.length} days`}>
          {/* recessive baseline, no grid: the readout carries the numbers */}
          <line x1={PADL} x2={W - PADR} y1={zero} y2={zero} stroke={GRID} strokeWidth="1" />
          <text x={PADL - 6} y={y(max) + 4} textAnchor="end" fontSize="10" fill={INK_MUTED}>{fmtV(max)}</text>
          {min < 0 && (
            <text x={PADL - 6} y={y(min) + 4} textAnchor="end" fontSize="10" fill={INK_MUTED}>{fmtV(min)}</text>
          )}
          {v.kind === 'line' ? (
            <>
              <path d={vals.map((n, i) => `${i ? 'L' : 'M'}${cx(i)},${y(n)}`).join(' ')}
                    fill="none"
                    stroke={v.tone === 'neutral' ? INK_MUTED : (vals[vals.length - 1] >= 0 ? POS : NEG)}
                    strokeWidth="2"
                    strokeLinejoin="round" strokeLinecap="round" />
              {vals.map((n, i) => (
                <circle key={i} cx={cx(i)} cy={y(n)} r={hi === i ? 5 : 0}
                        fill={n >= 0 ? POS : NEG} stroke="var(--surface)" strokeWidth="2" />
              ))}
            </>
          ) : (
            vals.map((n, i) => {
              const top = Math.min(zero, y(n))
              const h = Math.max(2, Math.abs(y(n) - zero))
              return (
                <rect key={i} x={cx(i) - bw / 2} y={top} width={bw} height={h} rx="3"
                      fill={v.tone === 'neutral' ? INK_MUTED : (n >= 0 ? POS : NEG)}
                      opacity={hi == null || hi === i ? 0.92 : 0.38} />
              )
            })
          )}
          {/* One direct label: the bar you are pointing at. Never all of them. */}
          {hi != null && (
            <text x={cx(hi)} y={Math.max(11, Math.min(zero, y(vals[hi])) - 5)}
                  textAnchor="middle" fontSize="11" fontWeight="600" fill={INK}>
              {fmtV(vals[hi])}
            </text>
          )}
          <text x={PADL} y={height - 5} fontSize="10" fill={INK_MUTED}>{rows[0].day?.slice(5)}</text>
          <text x={W - PADR} y={height - 5} fontSize="10" fill={INK_MUTED} textAnchor="end">
            {rows[rows.length - 1].day?.slice(5)}
          </text>
        </svg>
      </div>
    </div>
  )
}

export function BarChart({ bars, height = 220, valueFormat = fmtUsd }) {
  const wrap = useRef(null)
  const width = useSize(wrap)
  const [hover, setHover] = useState(null)
  const m = { top: 12, right: 12, bottom: 30, left: 56 }
  const iw = Math.max(60, width - m.left - m.right)
  const ih = height - m.top - m.bottom

  if (!bars || bars.length === 0) return <div className="mut">no data in this range</div>

  const vals = bars.map((b) => b.value)
  const yMax = Math.max(...vals, 0) * 1.15 || 1
  const yMin = Math.min(...vals, 0) * 1.15
  const sy = (v) => ih - ((v - yMin) / (yMax - yMin || 1)) * ih
  const bw = Math.max(2, (iw / bars.length) - 2)   // 2px surface gap between bars
  const zero = sy(0)

  return (
    <div ref={wrap} style={{ position: 'relative' }}>
      <div className="row" style={{ marginBottom: 6, fontSize: 11 }}>
        <span className="legend-item"><span className="legend-swatch" style={{ background: POS }} />profit</span>
        <span className="legend-item"><span className="legend-swatch" style={{ background: NEG }} />loss</span>
      </div>
      <svg width={width} height={height} role="img" aria-label="daily profit and loss">
        <g transform={`translate(${m.left},${m.top})`}>
          {[yMin, (yMin + yMax) / 2, yMax].map((t, i) => (
            <g key={i}>
              <line x1={0} x2={iw} y1={sy(t)} y2={sy(t)} stroke={GRID} strokeWidth={1} />
              <text x={-8} y={sy(t)} dy="0.32em" textAnchor="end" fontSize={11} fill={INK_MUTED}>
                {valueFormat(t)}
              </text>
            </g>
          ))}
          <line x1={0} x2={iw} y1={zero} y2={zero} stroke={AXIS} strokeWidth={1.5} />
          {bars.map((b, i) => {
            const x = i * (iw / bars.length)
            const y = b.value >= 0 ? sy(b.value) : zero
            const h = Math.max(1, Math.abs(sy(b.value) - zero))
            return (
              <rect key={i} x={x} y={y} width={bw} height={h} rx={3}
                    fill={b.value >= 0 ? POS : NEG}
                    onMouseEnter={() => setHover({ i, x: x + bw / 2, b })}
                    onMouseLeave={() => setHover(null)} />
            )
          })}
          <text x={0} y={ih + 20} fontSize={11} fill={INK_MUTED}>{bars[0].label}</text>
          <text x={iw} y={ih + 20} fontSize={11} fill={INK_MUTED} textAnchor="end">
            {bars[bars.length - 1].label}
          </text>
        </g>
      </svg>
      {hover && (
        <div className="tooltip" style={{ left: Math.min(hover.x + m.left, width - 170) }}>
          <div className="mut" style={{ fontSize: 11 }}>{hover.b.label}</div>
          <div className="mono">{valueFormat(hover.b.value)}</div>
          {hover.b.detail && <div className="mut" style={{ fontSize: 11 }}>{hover.b.detail}</div>}
        </div>
      )}
    </div>
  )
}
