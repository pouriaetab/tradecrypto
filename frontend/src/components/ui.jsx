import React, { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import CoinChart from './CoinChart.jsx'
import { api } from '../lib/api.js'

/** Hover-only explanation. Use for jargon and anything that was previously a
 *  paragraph of prose — the text is still there, it just is not taking space.
 *
 *  The popup is positioned FIXED, from the trigger's screen coordinates, rather
 *  than absolutely inside its parent. Every table on this dashboard sits in
 *  `.scroll { overflow-x: auto }`, and a scroll container clips anything drawn
 *  outside it — so an absolutely-positioned tooltip above a table header was
 *  being rendered correctly and then cut off, invisibly. Fixed positioning is
 *  relative to the viewport, so nothing can clip it.
 */
export function Info({ text, label }) {
  const ref = React.useRef(null)
  const [pos, setPos] = useState(null)
  const show = () => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const half = 140
    const below = window.innerHeight - r.bottom > 130
    setPos({
      left: Math.min(Math.max(r.left + r.width / 2, half + 8), window.innerWidth - half - 8),
      top: below ? r.bottom + 8 : undefined,
      bottom: below ? undefined : window.innerHeight - r.top + 8,
    })
  }
  const hide = () => setPos(null)
  // A fixed-position popup must be dismissed by more than mouseleave. If the
  // component re-renders under the cursor (these pages poll every few seconds),
  // or the page scrolls, mouseleave may never fire and the popup stays on
  // screen over unrelated content. Clear it on anything that could move it.
  React.useEffect(() => {
    if (!pos) return
    const off = () => setPos(null)
    window.addEventListener('scroll', off, true)
    window.addEventListener('resize', off)
    window.addEventListener('blur', off)
    document.addEventListener('visibilitychange', off)
    const t = setTimeout(off, 12000)
    return () => {
      window.removeEventListener('scroll', off, true)
      window.removeEventListener('resize', off)
      window.removeEventListener('blur', off)
      document.removeEventListener('visibilitychange', off)
      clearTimeout(t)
    }
  }, [pos])
  return (
    <span ref={ref} className="info" tabIndex={0} aria-label={text}
          onMouseEnter={show} onFocus={show} onMouseLeave={hide} onBlur={hide}>
      {label || 'i'}
      {pos && createPortal(
        <span className="info-pop" style={{
          display: 'block', position: 'fixed',
          left: pos.left, top: pos.top, bottom: pos.bottom,
          transform: 'translateX(-50%)',
        }}>{text}</span>,
        document.body)}
    </span>
  )
}

/** One short line, with the long version behind a disclosure. */
export function Hint({ children, more }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="hint">
      {children}
      {more && (
        <>
          {' '}
          <button className="linkish" onClick={() => setOpen((v) => !v)}>
            {open ? 'less' : 'why'}
          </button>
          {open && <div className="hint-more">{more}</div>}
        </>
      )}
    </div>
  )
}

/* A card with `collapse="some.id"` remembers whether you hid it. The choice is
 * per card and per browser, so the Daily tab can be trimmed to the sections
 * you read and stay that way across reloads. A hidden card keeps its title
 * line, so nothing disappears without a trace. */
function _loadOpen(id) {
  try {
    const v = localStorage.getItem('tc.card.' + id)
    return v === null ? true : v === '1'
  } catch { return true }
}

export function Card({ title, children, right, collapse }) {
  const [open, setOpen] = useState(() => (collapse ? _loadOpen(collapse) : true))
  const toggle = () => {
    if (!collapse) return
    setOpen((o) => {
      try { localStorage.setItem('tc.card.' + collapse, o ? '0' : '1') } catch { /* private mode */ }
      return !o
    })
  }
  return (
    <div className={`card${collapse ? ' collapsible' : ''}${collapse && !open ? ' collapsed' : ''}`}>
      {(title || right || collapse) && (
        <div className="row">
          {collapse && (
            <button type="button" className="card-toggle" onClick={toggle}
                    title={open ? 'hide this section (remembered)' : 'show this section'}
                    aria-expanded={open}>{open ? '▾' : '▸'}</button>
          )}
          {title && <h3 style={{ margin: 0, cursor: collapse ? 'pointer' : undefined }}
                        onClick={collapse ? toggle : undefined}>{title}</h3>}
          <div className="spacer" />
          {(open || !collapse) && right}
          {collapse && !open && <span className="mut" style={{ fontSize: 11 }}>hidden — click to show</span>}
        </div>
      )}
      {(open || !collapse) && children}
    </div>
  )
}

/**
 * The only component allowed to display a number.
 * It refuses to render a value whose sample is too small, and it always offers
 * the model card that produced it.
 */
export function Stat({ label, value, meta, n, minN = 30, model, onModel, tone, small }) {
  const insufficient = n != null && n < minN
  return (
    <div className="stat">
      <div className="label">{label}</div>
      {insufficient ? (
        <div className="insufficient">insufficient evidence</div>
      ) : (
        <div className={`value ${small ? 'small' : ''} ${tone || ''}`}>{value ?? '—'}</div>
      )}
      {(meta || n != null) && (
        <div className="meta">
          {n != null ? `n = ${n}${insufficient ? ` (need ${minN})` : ''}` : ''}
          {meta ? (n != null ? ' · ' : '') + meta : ''}
        </div>
      )}
      {model && (
        <button className="why" onClick={() => onModel(model)}>how is this computed?</button>
      )}
    </div>
  )
}

/* ── the symbol chart, available from every table on every page ───────────
 *
 * Journal had this as a 200-character inline `render` repeated three times, and
 * nowhere else had it at all: the same coin was clickable on one tab and dead
 * text on the next. One store, one host, one <Sym> -- and Table wires the
 * symbol column up on its own, so a page gets it without knowing it exists.
 */
let _openChart = () => {}
export function openSymbolChart(payload) { _openChart(payload) }

/** Derive the day/entry/exit a chart wants from whatever row we were given. */
export function chartArgsFor(row, symbol) {
  const ts = row?.ts ?? row?.ts_decided ?? row?.ts_close ?? row?.ts_open ?? null
  return {
    symbol: symbol ?? row?.symbol,
    day: ts
      ? new Date(ts * 1000).toLocaleDateString('en-CA', { timeZone: 'America/Chicago' })
      : null,                                  // null => the chart shows the most recent day
    entry: row?.entry_px ?? row?.fill_price ?? null,
    exit: row?.exit_px ?? null,
  }
}

/** A symbol you can press. Works in a table cell, a stat, or a sentence. */
export function Sym({ symbol, row, children }) {
  const sym = symbol ?? row?.symbol
  if (!sym) return <>{children ?? '—'}</>
  return (
    <button type="button" className="symlink"
            title={`chart for ${sym}`}
            onClick={(e) => { e.stopPropagation(); openSymbolChart(chartArgsFor(row, sym)) }}>
      {children ?? sym}
    </button>
  )
}

/** Mounted once, at the app root, so the chart is above every page and layer. */
export function SymbolChartHost() {
  const [pick, setPick] = useState(null)
  useEffect(() => {
    _openChart = (p) => setPick(p)
    return () => { _openChart = () => {} }
  }, [])
  if (!pick) return null
  return <CoinChart {...pick} onClose={() => setPick(null)} />
}

/* ── the shared table: pinning, filtering and a summary bar ──────────────────
 *
 * Which columns stay put when you scroll sideways.
 *
 * The DEFAULT is the leading RUN of "which row am I on" columns -- a date/time
 * and a symbol -- because "09-18" on its own does not tell you whose row this
 * is, and a few tables already pinned their first column on a phone and it was
 * the thing that made them readable.
 *
 * The CHOICE is the operator's, and it is now a CLICK ON THE HEADING rather
 * than a none/1/2 picker above the table. Any column can be pinned, in any
 * combination, and the ones you pick move to the front in the order you picked
 * them -- coin, then strategy, then held, and unpinning held takes it back out
 * without disturbing the other two.
 */
const KEY_COL = /^(ts|ts_[a-z_]+|time|day|date|when|closed|opened|symbol|coin|pair)$/i

function stickyCount(cols) {
  let n = 0
  while (n < cols.length && n < 2 && KEY_COL.test(cols[n].key)) n += 1
  // If the table does not start with one, pin the first column anyway: knowing
  // which row you are on matters more than which column it happens to be.
  return n || 1
}

/* ── what a table remembers, per table shape, per browser ────────────────────
 *
 * One key scheme for every choice: the column order, which columns are pinned,
 * which filters are on, and which numeric columns are being summarised. The
 * table's own column keys are its identity, so the settings follow that table
 * on that tab and nothing leaks between two different tables.
 *
 * Every read and write is wrapped. localStorage throws in private mode, and a
 * dashboard that will not paint because it could not remember a preference is a
 * worse bug than forgetting the preference. A malformed stored value is treated
 * as unset, never as a reason to throw.
 */
const shapeKey = (prefix, cols) => prefix + (cols || []).map((c) => c.key).join(',').slice(0, 120)
const PIN_KEY = (cols) => shapeKey('tc.pin.', cols)
const ORDER_KEY = (cols) => shapeKey('tc.order.', cols)
const FILTER_KEY = (cols) => shapeKey('tc.filter.', cols)
const STATS_KEY = (cols) => shapeKey('tc.stats.', cols)

function saveJSON(key, value) {
  try {
    if (value == null) localStorage.removeItem(key)
    else localStorage.setItem(key, JSON.stringify(value))
  } catch { /* private mode */ }
}

function readJSON(key) {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null || raw === '') return undefined
    return JSON.parse(raw)
  } catch { return undefined }
}

/* Pinned columns used to be a COUNT -- 0, 1 or 2 leading columns, from a
 * segmented picker. They are now the ordered LIST OF KEYS you clicked. An old
 * count is still out there in every browser that has used this app, so it is
 * read once, converted to the keys it actually meant, and written back in the
 * new shape. Anything else unreadable falls back to the default rather than
 * throwing: a stored preference must never be able to blank a page. */
function loadPins(cols, fallback) {
  const key = PIN_KEY(cols)
  let raw = null
  try { raw = localStorage.getItem(key) } catch { return fallback }
  if (raw == null) return fallback
  const text = String(raw).trim()
  if (/^\d+$/.test(text)) {
    const n = Math.max(0, Math.min(parseInt(text, 10), (cols || []).length))
    const keys = (cols || []).slice(0, n).map((c) => c.key)
    saveJSON(key, keys)                       // migrate in place, once
    return keys
  }
  let parsed
  try { parsed = JSON.parse(text) } catch { return fallback }
  if (!Array.isArray(parsed)) return fallback
  const have = new Set((cols || []).map((c) => c.key))
  const out = []
  for (const k of parsed) {
    if (typeof k === 'string' && have.has(k) && !out.includes(k)) out.push(k)
  }
  return out                                  // [] is a real answer: nothing pinned
}

function loadFilters(cols) {
  const blank = { filters: {}, q: '' }
  const v = readJSON(FILTER_KEY(cols))
  if (!v || typeof v !== 'object' || Array.isArray(v)) return blank
  const out = { filters: {}, q: '' }
  if (v.filters && typeof v.filters === 'object' && !Array.isArray(v.filters)) {
    for (const k of Object.keys(v.filters)) {
      const val = v.filters[k]
      if (typeof val === 'string' && val !== '') out.filters[k] = val
    }
  }
  if (typeof v.q === 'string') out.q = v.q.slice(0, 200)
  return out
}

function loadStatCols(cols) {
  const v = readJSON(STATS_KEY(cols))
  if (!Array.isArray(v)) return []
  const have = new Set((cols || []).map((c) => c.key))
  const out = []
  for (const k of v) if (typeof k === 'string' && have.has(k) && !out.includes(k)) out.push(k)
  return out
}

/* Column ORDER is the operator's too. Drag a header onto another to move it;
 * the order is remembered per table shape (same key scheme as pinning), so it
 * survives reloads and applies to that table in that tab -- and only there.
 * Pinning is applied on top of the reordered columns. */
function loadOrder(cols) {
  try {
    const raw = localStorage.getItem(ORDER_KEY(cols))
    if (!raw) return null
    const keys = JSON.parse(raw)
    const have = new Set(cols.map((c) => c.key))
    if (!Array.isArray(keys) || keys.length !== cols.length || !keys.every((k) => have.has(k))) return null
    return keys
  } catch { return null }
}

function applyOrder(cols, keys) {
  if (!keys) return cols
  const by = new Map(cols.map((c) => [c.key, c]))
  return keys.map((k) => by.get(k)).filter(Boolean)
}

/* ── reading a number out of a row ───────────────────────────────────────────
 *
 * The summary bar is computed from the ROW VALUE, never from what the column
 * renders. `fmt.usd(-3.1)` is the string "$-3.10" and `fmt.bps` is "-3 bps";
 * adding those up is how a total comes out wrong and confident. So: take
 * r[col.key], and only if that is itself a formatted string do we strip the
 * formatting. Anything that still will not parse is left out of the column's
 * stats entirely and shows up as a smaller n.
 */
const NUMERIC_TEXT = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/

function toNum(v) {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v !== 'string') return null      // null, undefined, objects, booleans
  let s = v.trim()
  if (!s) return null
  let sign = 1
  if (/^\(.*\)$/.test(s)) { sign = -1; s = s.slice(1, -1).trim() }   // (1.20) is -1.20
  s = s.replace(/[$\u20AC\u00A3,\s]/g, '')                          // currency, thousands, spaces
  s = s.replace(/(%|bps|bp|x)$/i, '')                               // trailing units
  s = s.replace(/^\u2212/, '-')                                     // unicode minus
  if (!NUMERIC_TEXT.test(s)) return null                            // "—", "n/a", free text
  const n = Number(s)
  return Number.isFinite(n) ? sign * n : null
}

/** The text of a cell, for filtering and searching. Objects have no text. */
function cellText(row, key, col) {
  const v = col ? colValue(col, row) : (row ? row[key] : undefined)
  if (v == null) return ''
  const t = typeof v
  if (t === 'string') return v
  if (t === 'number' || t === 'boolean') return String(v)
  return ''
}

/* Which columns can be filtered and which can be summed, decided by the SHAPE
 * of the data and never by a column's name. A name-based list would work on the
 * tables it was written against and quietly do nothing on the next one.
 *
 *   filterable -- every non-empty value is a string, there are between 2 and 25
 *                 distinct ones, there are fewer distinct values than rows (a
 *                 column where every row differs is an identifier, not a
 *                 category), and none of them is long enough to be free text.
 *   numeric    -- at least one value parses as a number and at least 80% of the
 *                 non-empty ones do. Identity columns are left out: summing a
 *                 timestamp is arithmetic, not information.
 */
const FILTER_MAX_DISTINCT = 25
const FILTER_MAX_LEN = 40
const NUMERIC_SHARE = 0.8

/* A column's value for FILTERING and STATS -- which is not always `row[key]`.
 *
 * 2026-09-21: the Journal's "short by" column is computed inside its render
 * (expected_edge_bps - cost_hurdle_bps) and never written to the row, so
 * row['gap'] was undefined, the column failed numeric detection, and it could
 * not be summed. The operator found it. Any column whose number only exists
 * inside render is invisible here, so a column may now declare `value(row)`
 * and that is what the detector, the filter and the stats all read.
 *
 * `render` is deliberately NOT used as a fallback: it returns JSX, and parsing
 * a number back out of rendered markup is how a table starts summing the wrong
 * thing silently. */
function colValue(c, r) {
  if (!r) return undefined
  if (typeof c.value === 'function') {
    try { return c.value(r) } catch { return undefined }
  }
  return r[c.key]
}

function describeColumns(cols, rows) {
  const filterable = []
  const numeric = []
  for (const c of cols) {
    let seen = 0, strings = 0, nums = 0, longest = 0, overflow = false
    const distinct = new Map()
    for (const r of rows) {
      const v = colValue(c, r)
      if (v == null || v === '') continue
      seen += 1
      if (typeof v === 'string') {
        strings += 1
        if (v.length > longest) longest = v.length
        if (!overflow) {
          distinct.set(v, (distinct.get(v) || 0) + 1)
          if (distinct.size > FILTER_MAX_DISTINCT) { overflow = true; distinct.clear() }
        }
      }
      if (toNum(v) !== null) nums += 1
    }
    if (seen > 0 && nums > 0 && nums >= NUMERIC_SHARE * seen && !KEY_COL.test(c.key)) {
      numeric.push({ key: c.key, label: c.label || c.key })
      continue
    }
    if (!overflow && strings === seen && distinct.size >= 2
        && distinct.size < seen && longest <= FILTER_MAX_LEN) {
      filterable.push({
        key: c.key,
        label: c.label || c.key,
        values: Array.from(distinct.keys()).sort(),
      })
    }
  }
  return { filterable, numeric }
}

/** n, sum, mean, min, max over whatever rows are on screen right now. */
function summarise(rows, col) {
  let n = 0, sum = 0, min = Infinity, max = -Infinity, missing = 0
  for (const r of rows) {
    const v = toNum(colValue(col, r))
    if (v === null) { missing += 1; continue }
    n += 1
    sum += v
    if (v < min) min = v
    if (v > max) max = v
  }
  return {
    // `key` was a PARAMETER until summarise() was changed to take the whole
    // column (so a computed column could declare its own value accessor). The
    // signature changed; this line did not, and `key` became a reference to
    // nothing -- "key is not defined", thrown the moment anyone asked for a
    // total. Neither render check caught it: the stats are computed on a CLICK,
    // and both checks render the page at rest.
    key: col && col.key,
    n,
    missing,
    sum: n ? sum : null,
    mean: n ? sum / n : null,
    min: n ? min : null,
    max: n ? max : null,
  }
}

/* One formatter for every summary figure, because a table can hold ZEC at 1,133
 * and BONK at 0.00000317 in the same column. Precision follows magnitude, the
 * same way fmt.px does for prices. */
function fmtStat(v) {
  if (v == null || !Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a === 0) return '0'
  if (a >= 1e9 || a < 1e-4) return v.toExponential(2)
  if (a >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (a >= 1) return v.toFixed(2)
  return v.toPrecision(4)
}

/* The table controls need :hover and :focus-visible, which an inline style
 * cannot express, so they need real CSS. It belongs next to .pin-bar and
 * .stick-col in styles/styles.css and should be moved there -- it lives here
 * only because this component was changed on its own. Every custom property it
 * uses is one styles.css already defines (check_css_vars.py enforces that).
 * Injected once per document, not once per table. */
const TABLE_CSS = `
th.th-pin { cursor: pointer; }
th.th-pin:hover { color: var(--text); background: var(--surface-2); }
th.th-pin:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
th.th-pin.pinned { color: var(--text); background: var(--surface-2); }
th.th-pin.dragging { cursor: grabbing; }
/* The mark is part of the heading, not a target of its own: a click landing
   on it must still reach the <th> that toggles the pin. */
th.th-pin .th-mark { color: var(--accent); margin-right: 3px; font-size: 10px; pointer-events: none; }
.pin-bar input.tc-find, .pin-bar select.tc-pick {
  min-height: 0; padding: 2px 6px; font-size: 11px; border-radius: 6px;
}
.pin-bar input.tc-find { width: 150px; max-width: 100%; }
.pin-bar select.tc-pick { max-width: 45vw; }
.pin-bar .tc-stat { color: var(--muted); font-variant-numeric: tabular-nums; }
.pin-bar .tc-stat b { color: var(--text); font-weight: 600; }
.pin-bar .tc-stat i { color: var(--text); font-style: normal; }
.pin-bar .tc-note { color: var(--warning); }
@media (max-width: 560px) {
  .pin-bar input.tc-find, .pin-bar select.tc-pick { min-height: 38px; font-size: 16px; padding: 4px 8px; }
  .pin-bar input.tc-find { width: 100%; }
  .pin-bar select.tc-pick { max-width: 100%; }
}
`
let _tableCssDone = false
function useTableCss() {
  useEffect(() => {
    if (_tableCssDone) return
    try {
      const el = document.createElement('style')
      el.setAttribute('data-tc', 'table')
      el.textContent = TABLE_CSS
      document.head.appendChild(el)
      _tableCssDone = true
    } catch { /* no document: a server render, where styles do not apply anyway */ }
  }, [])
}

/* There is NO cap on how many columns may be pinned.
 *
 * There was one -- 60% of the table's width, past which the newest pin was
 * refused. The reasoning was that a date plus a symbol can eat half a 390px
 * screen and leave a sliver of the numbers you were scrolling across to read.
 * That is a real thing that can happen; it is also the operator's business, not
 * the table's. 2026-09-22: "please remove the restriction so i can select any
 * amount of column i want and still be able to scroll to the right or left".
 *
 * Pinning every column is a legitimate choice -- it is how you get a table that
 * simply does not scroll sideways. What the component owes him is that the
 * table KEEPS WORKING at any number of pins: the sticky offsets are measured
 * rather than assumed, so column n+1 starts where column n ends whatever the
 * widths are, and the body scrolls under them normally. That is tested. */

/** A table whose columns change is a different table: its remembered order,
 *  pins, filters and stats belong to the new shape. Keying on the shape starts
 *  the view below again rather than leaving it holding keys that no longer
 *  exist. Everything it had is in localStorage under the old shape's key. */
export function Table(props) {
  const shape = (props.cols || []).map((c) => c.key).join(',')
  return <TableView key={shape} {...props} />
}

function TableView({ cols: colsIn, rows: rowsIn, empty = 'nothing yet' }) {
  const declared = colsIn || []
  const rows = React.useMemo(() => rowsIn || [], [rowsIn])
  useTableCss()

  // ── column order ──────────────────────────────────────────────────────────
  const [order, setOrder] = useState(() => loadOrder(declared))
  const [drag, setDrag] = useState(null)      // key being dragged
  const [over, setOver] = useState(null)      // key it is currently above
  const ordered = React.useMemo(() => applyOrder(colsIn || [], order), [colsIn, order])

  // ── which columns are pinned ──────────────────────────────────────────────
  const [pins, setPins] = useState(() =>
    loadPins(declared, ordered.slice(0, stickyCount(ordered)).map((c) => c.key)))
  // Keys that are no longer columns are ignored rather than removed, so a table
  // that briefly arrives with fewer columns does not forget the rest.
  const pinned = React.useMemo(() => {
    const have = new Set(ordered.map((c) => c.key))
    return pins.filter((k) => have.has(k))
  }, [pins, ordered])
  const nSticky = pinned.length

  // Pinned columns are MOVED to the front, in the order they were clicked, and
  // made sticky there. That is what "I select coin, then strategy, then held"
  // has to mean: the three of them together, in that order, on the left.
  const cols = React.useMemo(() => {
    if (!pinned.length) return ordered
    const by = new Map(ordered.map((c) => [c.key, c]))
    const set = new Set(pinned)
    return pinned.map((k) => by.get(k)).filter(Boolean)
      .concat(ordered.filter((c) => !set.has(c.key)))
  }, [ordered, pinned])

  const labelOf = (key) => {
    const c = declared.find((x) => x.key === key)
    return c ? (c.label || c.key) : key
  }

  const saveOrder = (keys) => {
    setOrder(keys)
    try {
      if (keys) localStorage.setItem(ORDER_KEY(declared), JSON.stringify(keys))
      else localStorage.removeItem(ORDER_KEY(declared))
    } catch { /* private mode */ }
  }
  const savePins = (keys) => {
    setPins(keys)
    saveJSON(PIN_KEY(declared), keys)
  }

  // Dragging one pinned column onto another reorders the PINNED list -- that is
  // what the gesture means there, and the alternative is a column that springs
  // back to where it was. Everything else moves in the underlying column order.
  const moveCol = (fromKey, toKey) => {
    if (!fromKey || !toKey || fromKey === toKey) return
    if (pinned.includes(fromKey) && pinned.includes(toKey)) {
      const next = pinned.slice()
      const from = next.indexOf(fromKey), to = next.indexOf(toKey)
      next.splice(from, 1); next.splice(to, 0, fromKey)
      savePins(next)
      return
    }
    const keys = ordered.map((c) => c.key)
    const from = keys.indexOf(fromKey), to = keys.indexOf(toKey)
    if (from < 0 || to < 0) return
    keys.splice(from, 1); keys.splice(to, 0, fromKey)
    saveOrder(keys)
  }

  // A click that was part of a drag must not toggle a pin. Two independent
  // guards, because either one alone has a hole: a drag that ends on the
  // heading it started on may still deliver a click, and a press that moves a
  // few pixels without ever starting a drag is a slip, not a choice.
  const dragged = React.useRef(false)
  const press = React.useRef(null)
  const dragProps = (c) => ({
    draggable: true,
    onDragStart: (e) => {
      dragged.current = true
      setDrag(c.key)
      try { e.dataTransfer.setData('text/plain', c.key) } catch { /* ignore */ }
      e.dataTransfer.effectAllowed = 'move'
    },
    onDragOver: (e) => { e.preventDefault(); if (over !== c.key) setOver(c.key) },
    onDragLeave: () => { if (over === c.key) setOver(null) },
    onDrop: (e) => { e.preventDefault(); moveCol(drag, c.key); setDrag(null); setOver(null) },
    onDragEnd: () => { setDrag(null); setOver(null) },
  })

  const headRef = React.useRef(null)
  const wrapRef = React.useRef(null)
  const widths = React.useRef(new Map())      // column key -> measured header width
  const [offsets, setOffsets] = useState([0])
  const [wide, setWide] = useState(false)
  const [note, setNote] = useState(null)

  // Only offer the sideways-scrolling hints when the table actually scrolls
  // sideways — on a table that fits, they are clutter explaining a problem you
  // do not have.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const check = () => setWide(el.scrollWidth > el.clientWidth + 4)
    check()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(check)
    ro.observe(el)
    return () => ro.disconnect()
  }, [cols, rows])

  // Each pinned column has to start where the one before it ends, and that width
  // depends on the content, the font and the viewport. Measure it rather than
  // guessing a number that is wrong on a phone. The same pass records EVERY
  // header's width, which is what the 60% cap is measured against.
  useEffect(() => {
    const el = headRef.current
    if (!el) return
    const measure = () => {
      const cells = Array.from(el.children)
      const w = new Map()
      const next = []
      let run = 0
      cells.forEach((cell, i) => {
        const width = cell.getBoundingClientRect().width
        if (cols[i]) w.set(cols[i].key, width)
        if (i < nSticky) { next.push(run); run += width }
      })
      widths.current = w
      setOffsets((prev) =>
        prev.length === next.length && prev.every((v, i) => Math.abs(v - next[i]) < 0.5)
          ? prev : next)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    Array.from(el.children).forEach((cell) => ro.observe(cell))
    return () => ro.disconnect()
  }, [nSticky, cols, rows])

  // The refusal note is a moment's explanation, not a permanent banner.
  useEffect(() => {
    if (!note) return
    const t = setTimeout(() => setNote(null), 7000)
    return () => clearTimeout(t)
  }, [note])

  const togglePin = (key) => {
    setNote(null)
    if (pinned.includes(key)) { savePins(pinned.filter((k) => k !== key)); return }
    // Any number. See the comment on the removed cap above.
    savePins(pinned.concat([key]))
  }

  const onHeadPointerDown = (e) => {
    dragged.current = false
    press.current = { x: e.clientX, y: e.clientY }
  }
  const onHeadClick = (e, key) => {
    if (e.target !== e.currentTarget) return   // the <Info> hover marker, not the heading
    const p = press.current
    press.current = null
    if (dragged.current || drag) { dragged.current = false; return }
    if (p && (Math.abs(e.clientX - p.x) > 6 || Math.abs(e.clientY - p.y) > 6)) return
    togglePin(key)
  }
  const onHeadKey = (e, key) => {
    if (e.target !== e.currentTarget) return
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault()
      togglePin(key)
    }
  }

  // ── filtering ─────────────────────────────────────────────────────────────
  const [filterState, setFilterState] = useState(() => loadFilters(declared))
  const filters = filterState.filters
  const q = filterState.q
  const applyFilterState = (next) => {
    setFilterState(next)
    saveJSON(FILTER_KEY(declared), next)
  }
  const setFilter = (key, value) => {
    const f = { ...filters }
    if (value) f[key] = value; else delete f[key]
    applyFilterState({ filters: f, q })
  }
  const setQuery = (value) => applyFilterState({ filters, q: value })
  const clearFilters = () => applyFilterState({ filters: {}, q: '' })

  // Detection reads the DECLARED columns, not the displayed ones, so pinning or
  // reordering a table does not make its filters flicker in and out.
  const { filterable, numeric } = React.useMemo(
    () => describeColumns(colsIn || [], rows), [colsIn, rows])
  const colKeys = React.useMemo(() => new Set(declared.map((c) => c.key)), [colsIn]) // eslint-disable-line
  // Column objects by key, so filtering and stats can reach a column's own
  // value accessor rather than assuming the number sits at row[key].
  const byKey = React.useMemo(() => new Map(declared.map((c) => [c.key, c])), [declared])
  // A stored filter keeps applying as long as its column still exists, even if
  // the data has changed shape enough that the column is no longer offered.
  // A filter that silently stopped filtering would be the worse surprise.
  const activeFilters = React.useMemo(
    () => Object.keys(filters).filter((k) => colKeys.has(k) && filters[k]).map((k) => [k, filters[k]]),
    [filters, colKeys])
  const needle = q.trim().toLowerCase()
  const searchKeys = React.useMemo(() => declared.map((c) => c.key), [colsIn]) // eslint-disable-line

  const view = React.useMemo(() => {
    let out = rows
    for (const [k, v] of activeFilters) out = out.filter((r) => cellText(r, k, byKey.get(k)) === v)
    if (needle) {
      out = out.filter((r) => searchKeys.some((k) => cellText(r, k, byKey.get(k)).toLowerCase().includes(needle)))
    }
    return out
  }, [rows, activeFilters, needle, searchKeys])

  // Counts next to each choice are counted over the rows the OTHER filters
  // leave, so they say what picking that value would actually give you.
  const facets = React.useMemo(() => {
    const out = new Map()
    for (const f of filterable) {
      const counts = new Map()
      for (const r of rows) {
        let keep = true
        for (const [k, v] of activeFilters) {
          if (k !== f.key && cellText(r, k, byKey.get(k)) !== v) { keep = false; break }
        }
        if (keep && needle) {
          keep = searchKeys.some((k) => cellText(r, k, byKey.get(k)).toLowerCase().includes(needle))
        }
        if (!keep) continue
        const v = cellText(r, f.key, byKey.get(f.key))
        if (v) counts.set(v, (counts.get(v) || 0) + 1)
      }
      out.set(f.key, counts)
    }
    return out
  }, [rows, filterable, activeFilters, needle, searchKeys])

  // ── the summary bar ───────────────────────────────────────────────────────
  const [statCols, setStatCols] = useState(() => loadStatCols(declared))
  const [statsOpen, setStatsOpen] = useState(false)
  const activeStats = React.useMemo(() => {
    const ok = new Set(numeric.map((c) => c.key))
    return statCols.filter((k) => ok.has(k))
  }, [statCols, numeric])
  const toggleStat = (key) => {
    const next = statCols.includes(key) ? statCols.filter((k) => k !== key) : statCols.concat([key])
    setStatCols(next)
    saveJSON(STATS_KEY(declared), next)
  }
  // Every figure is over the rows on screen, so it moves when a filter moves.
  const stats = React.useMemo(
    () => activeStats.map((k) => summarise(view, byKey.get(k) || { key: k })), [activeStats, view, byKey])

  const stickyClass = (i) =>
    (i < nSticky ? (i === nSticky - 1 ? 'stick-col stick-last' : 'stick-col') : '')

  // The body is memoised on its own: these tables run to thousands of rows, and
  // hovering a heading or opening a dropdown must not re-render all of them.
  const body = React.useMemo(() => view.map((r, i) => (
    <tr key={i}>
      {cols.map((c, ci) => (
        <td key={c.key}
            className={[c.num ? 'num' : '', stickyClass(ci)].filter(Boolean).join(' ')}
            style={ci < nSticky ? { left: offsets[ci] ?? 0 } : undefined}>
          {/* A symbol column becomes a chart button automatically. A column that
              renders itself is left alone -- it may already be doing something
              better -- unless it asks with symbol:true. */}
          {c.render
            ? (c.symbol ? <Sym row={r}>{c.render(r)}</Sym> : c.render(r))
            : ((c.key === 'symbol' || c.symbol) && r[c.key]
                ? <Sym row={r} symbol={r[c.key]} />
                : (r[c.key] ?? '—'))}
        </td>
      ))}
    </tr>
  )), [view, cols, nSticky, offsets]) // eslint-disable-line

  if (rows.length === 0) return <div className="mut">{empty}</div>

  const filtering = activeFilters.length > 0 || needle.length > 0
  // A one-line control bar on a three-row table is clutter. Offer filtering once
  // there is enough to hide something behind, and always offer the free-text
  // search on a table long enough to lose something in.
  const showFilters = rows.length >= 6 && (filterable.length > 0 || rows.length >= 25)
  const showStats = rows.length >= 6 && numeric.length > 0
  const pinLabel = nSticky === 0
    ? 'nothing pinned — click a heading to keep it on screen'
    : cols.slice(0, nSticky).map((c) => c.label || c.key).join(' + ')
      + ' pinned — click a heading to pin or unpin it'

  return (
    <>
      {showFilters && (
        <div className="pin-bar">
          <input type="search" className="tc-find" value={q}
                 placeholder="search this table"
                 aria-label="search every column of this table"
                 title="Matches anywhere in any column, ignoring case"
                 onChange={(e) => setQuery(e.target.value)} />
          {filterable.map((f) => {
            const counts = facets.get(f.key) || new Map()
            const picked = filters[f.key] || ''
            return (
              <label key={f.key} className="mut" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                {f.label}
                <select className="tc-pick" value={picked}
                        aria-label={`filter by ${f.label}`}
                        title={`Show only the rows whose ${f.label} is the one you pick`}
                        onChange={(e) => setFilter(f.key, e.target.value)}>
                  <option value="">all</option>
                  {f.values.map((v) => (
                    <option key={v} value={v}>{v} ({counts.get(v) || 0})</option>
                  ))}
                </select>
              </label>
            )
          })}
          {filtering && (
            <>
              <span className="mut">showing {view.length} of {rows.length} rows</span>
              <button type="button" className="tiny"
                      title="Drop every filter and the search box and show all the rows again"
                      onClick={clearFilters}>clear filters</button>
            </>
          )}
        </div>
      )}

      {showStats && (
        <div className="pin-bar">
          <button type="button" className="tiny" aria-expanded={statsOpen}
                  title="Pick the numeric columns to summarise. Every figure is over the rows you are looking at, so it follows the filters."
                  onClick={() => setStatsOpen((v) => !v)}>
            {statsOpen ? '▾' : '▸'} stats
          </button>
          {statsOpen && (
            <div className="seg">
              {numeric.map((c) => (
                <button key={c.key} type="button"
                        className={activeStats.includes(c.key) ? 'on' : ''}
                        aria-pressed={activeStats.includes(c.key)}
                        title={`n, sum, mean, min and max of ${c.label}, over the rows on screen`}
                        onClick={() => toggleStat(c.key)}>{c.label}</button>
              ))}
            </div>
          )}
          {statsOpen && activeStats.length === 0 && (
            <span className="mut">pick a column to summarise</span>
          )}
          {stats.map((s) => (
            <span key={s.key} className="tc-stat"
                  title={s.missing
                    ? `${s.missing} of the ${view.length} rows on screen hold no number here and are not counted`
                    : `over the ${view.length} rows on screen`}>
              <b>{labelOf(s.key)}</b>{' '}n <i>{s.n}</i>{' · '}sum <i>{fmtStat(s.sum)}</i>
              {' · '}mean <i>{fmtStat(s.mean)}</i>{' · '}min <i>{fmtStat(s.min)}</i>
              {' · '}max <i>{fmtStat(s.max)}</i>
            </span>
          ))}
        </div>
      )}

      {(wide || order || note) && (
        <div className="pin-bar">
          <span className="mut">{wide ? pinLabel : 'columns reordered'}</span>
          {note && <span className="tc-note" role="status">{note}</span>}
          {order && (
            <button type="button" className="tiny" title="Put the columns back in their original order"
                    onClick={() => saveOrder(null)}>reset columns</button>
          )}
        </div>
      )}

    <div className="scroll" ref={wrapRef}>
      <table>
        <thead>
          <tr ref={headRef}>{cols.map((c, i) => {
            const isPinned = i < nSticky
            const label = c.label || c.key
            return (
              <th key={c.key}
                  {...dragProps(c)}
                  role="button"
                  tabIndex={0}
                  aria-pressed={isPinned}
                  title={(isPinned
                    ? `${label} is pinned — click to unpin it`
                    : `click to pin ${label} to the left while you scroll sideways`)
                    + ' · drag to move this column'}
                  className={['th-pin', c.num ? 'num' : '', stickyClass(i),
                              isPinned ? 'pinned' : '',
                              drag === c.key ? 'dragging' : '',
                              over === c.key && drag && drag !== c.key ? 'drop-here' : ''].filter(Boolean).join(' ')}
                  style={isPinned ? { left: offsets[i] ?? 0 } : undefined}
                  onPointerDown={onHeadPointerDown}
                  onClick={(e) => onHeadClick(e, c.key)}
                  onKeyDown={(e) => onHeadKey(e, c.key)}>
                {isPinned && <span className="th-mark" aria-hidden="true">⚑</span>}
                {label}{c.info && <Info text={c.info} />}
              </th>
            )
          })}</tr>
        </thead>
        <tbody>
          {body}
        </tbody>
      </table>
      {view.length === 0 && (
        <div className="mut" style={{ padding: '8px 2px' }}>
          no rows match — <button type="button" className="linkish" onClick={clearFilters}>clear filters</button>
        </div>
      )}
    </div>
    </>
  )
}

export function Banner({ kind = 'info', title, children }) {
  return <div className={`banner ${kind}`}>{title && <b>{title}</b>}{children}</div>
}

export function ModelDrawer({ name, onClose }) {
  const [card, setCard] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    if (!name) return
    api.models().then((cards) => {
      const c = cards.find((x) => x.name === name)
      if (!c) setErr(`no model card named "${name}"`)
      setCard(c)
    }).catch((e) => setErr(e.message))
  }, [name])
  if (!name) return null
  return (
    <>
      <div className="backdrop" onClick={onClose} />
      <div className="drawer">
        <div className="row">
          <h2>{card?.name || name}</h2>
          <div className="spacer" />
          <button onClick={onClose}>close</button>
        </div>
        {err && <div className="err">{err}</div>}
        {card && (
          <>
            <div className="row" style={{ marginTop: 8 }}>
              <span className={`pill ${card.status === 'validated' ? 'ok' : 'warn'}`}>{card.status}</span>
              <span className="pill">v{card.version}</span>
              <span className="mut mono" style={{ fontSize: 11 }}>{card.code_ref}</span>
            </div>
            <div className="k">Question it answers</div><div>{card.question}</div>
            <div className="k">In plain english</div><div>{card.plain_english}</div>
            <div className="k">Formula</div><pre>{card.formula}</pre>
            <div className="k">Parameters</div><pre>{JSON.stringify(card.parameters, null, 2)}</pre>
            <div className="k">Assumptions</div>
            <ul>{card.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
            <div className="k">This breaks when</div>
            <ul>{card.breaks_when.map((a, i) => <li key={i}>{a}</li>)}</ul>
            <div className="k">Validation status</div><div>{card.validation}</div>
            {card.citations?.length > 0 && (<>
              <div className="k">References</div>
              <ul>{card.citations.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>)}
            <div className="k">Inputs → outputs</div>
            <div className="mut">{card.inputs.join(', ')} → {card.outputs.join(', ')}</div>
          </>
        )}
      </div>
    </>
  )
}

/* Everything every page has ever fetched, kept outside React.
 *
 * Switching tabs unmounts the page, so each hook started again from null and the
 * screen went blank for a second or two on every single tab change -- including
 * going BACK to a tab whose data was on screen moments earlier. The data had not
 * changed; only the component had gone away.
 *
 * This keeps the last answer for every distinct request, so a revisited tab
 * paints instantly with what it had and refreshes underneath. Stale for a
 * moment beats empty for two seconds, and nothing here is so time-critical that
 * a second-old number misleads. Anything that must be current carries an
 * intervalMs and keeps polling as before.
 *
 * The key is the fetcher's own source plus its dependencies: stable for a given
 * call site, different between call sites, no bookkeeping to forget.
 */
const _cache = new Map()
const CACHE_MAX = 300

function _cacheKey(fn, deps) {
  try { return fn.toString() + '|' + JSON.stringify(deps) } catch { return null }
}

// A failed load is a temporary state, not a dead end. The backend restarts in
// seconds; a tab that was open during one should come back on its own rather
// than sitting there until somebody reloads it.
const RETRY_BASE_MS = 2000
const RETRY_MAX_MS = 30000
// Last line of defence. Whatever `fn` is -- an api call, something else, a
// promise that simply never settles -- the spinner gets a deadline. A spinner
// with no deadline is the bug this whole hook exists to prevent.
const HANG_GUARD_MS = 45000

export function useAsync(fn, deps = [], intervalMs) {
  const key = _cacheKey(fn, deps)
  const cached = key ? _cache.get(key) : undefined
  const [data, setData] = useState(cached !== undefined ? cached : null)
  const [err, setErr] = useState(null)
  // Only "loading" when there is genuinely nothing to show.
  const [loading, setLoading] = useState(cached === undefined)
  const alive = React.useRef(true)
  const attempts = React.useRef(0)
  const retryTimer = React.useRef(null)
  const hangTimer = React.useRef(null)

  const clearTimers = React.useCallback(() => {
    if (retryTimer.current) { clearTimeout(retryTimer.current); retryTimer.current = null }
    if (hangTimer.current) { clearTimeout(hangTimer.current); hangTimer.current = null }
  }, [])

  useEffect(() => () => { alive.current = false; clearTimers() }, [clearTimers])

  const run = React.useCallback((background = false) => {
    const k0 = _cacheKey(fn, deps)
    if (!background && k0 && _cache.get(k0) === undefined) setLoading(true)

    if (hangTimer.current) clearTimeout(hangTimer.current)
    hangTimer.current = setTimeout(() => {
      if (!alive.current) return
      setLoading(false)
      setErr((prev) => prev || 'this is taking longer than it should — still retrying')
    }, HANG_GUARD_MS)

    return fn().then((d) => {
      if (!alive.current) return
      const k = _cacheKey(fn, deps)
      if (k) {
        if (_cache.size > CACHE_MAX) _cache.clear()
        _cache.set(k, d)
      }
      attempts.current = 0
      setData(d); setErr(null)
    })
      .catch((e) => {
        if (!alive.current) return
        setErr(e.message)
        attempts.current += 1
        const wait = Math.min(RETRY_BASE_MS * 2 ** (attempts.current - 1), RETRY_MAX_MS)
        if (retryTimer.current) clearTimeout(retryTimer.current)
        retryTimer.current = setTimeout(() => { if (alive.current) run(true) }, wait)
      })
      .finally(() => {
        if (hangTimer.current) { clearTimeout(hangTimer.current); hangTimer.current = null }
        if (alive.current) setLoading(false)
      })
  }, deps) // eslint-disable-line

  useEffect(() => {
    alive.current = true
    const k = _cacheKey(fn, deps)
    const hit = k ? _cache.get(k) : undefined
    if (hit !== undefined) { setData(hit); setLoading(false) }
    run(hit !== undefined)          // revalidate quietly when we already had something
    const id = intervalMs ? setInterval(() => run(true), intervalMs) : null
    return () => { if (id) clearInterval(id); clearTimers() }
  }, [run, intervalMs, clearTimers]) // eslint-disable-line

  return { data, err, loading, reload: () => { attempts.current = 0; return run(true) } }
}

/** Drop everything cached — after a reset, or any action that invalidates the app. */
export function clearAsyncCache() { _cache.clear() }
