import React, { useState, useEffect } from 'react'
import ErrorBoundary from './components/ErrorBoundary.jsx'
import FirstRunGate from './components/FirstRun.jsx'
import { ModelDrawer, SymbolChartHost } from './components/ui.jsx'
import { api, onConnection, isOnline, offlineReason, needsToken, onTokenNeeded, setAccessToken,
         denyReason, verifyToken } from './lib/api.js'
import ModeSwitch from './components/ModeSwitch.jsx'
import Daily from './pages/Daily.jsx'
import Overview from './pages/Overview.jsx'
import Sources from './pages/Sources.jsx'
import TradeDesk from './pages/TradeDesk.jsx'
import ModelLab from './pages/ModelLab.jsx'
import Ledger from './pages/Ledger.jsx'
import DataPage from './pages/Data.jsx'
import Attention from './pages/Attention.jsx'
import Automation from './pages/Automation.jsx'
import Universe from './pages/Universe.jsx'
import Breakout from './pages/Breakout.jsx'
import NewsAndPapers from './pages/Research2.jsx'
import Movers from './pages/Movers.jsx'
import DayScan from './pages/DayScan.jsx'
import CostLab from './pages/CostLab.jsx'
import Research from './pages/Research.jsx'
import Strategies from './pages/Strategies.jsx'
import Models from './pages/Models.jsx'
import Journal from './pages/Journal.jsx'
import TestLab from './pages/TestLab.jsx'
import TokenLedger from './pages/TokenLedger.jsx'
import VenueLab from './pages/VenueLab.jsx'
import Setup from './pages/Setup.jsx'
import Risk from './pages/Risk.jsx'
import SignalRace from './pages/SignalRace.jsx'

const PAGES = [
  ['setup', 'Setup', Setup],
  ['overview', 'Overview', Overview],
  ['daily', 'Daily', Daily],
  ['desk', 'Trade Desk', TradeDesk],
  ['modellab', 'Model Lab', ModelLab],
  ['news', 'News & Research', NewsAndPapers],
  ['movers', 'Movers', Movers],
  ['dayscan', 'Day Scan', DayScan],
  ['universe', 'Universe', Universe],
  ['automation', 'Automation', Automation],
  ['data', 'Data', DataPage],
  ['sources', 'Sources', Sources],
  ['strategies', 'Strategies', Strategies],
  ['journal', 'Journal', Journal],
  ['testlab', 'Test Lab', TestLab],
  ['venuelab', 'Venue Lab', VenueLab],
  ['risk', 'Risk', Risk],
  ['signals', 'Signals', SignalRace],
]

/** Says out loud when the backend is unreachable, instead of leaving a spinner.
 *
 * The backend restarts (planned or otherwise) take a few seconds. During one,
 * every request fails and every page would otherwise show nothing but a
 * spinner with no explanation. This states what is happening, keeps checking,
 * and disappears by itself the moment the backend answers again.
 */
function ConnectionBanner() {
  const [online, setOnline] = useState(isOnline())

  useEffect(() => onConnection(setOnline), [])

  useEffect(() => {
    if (online) return
    const id = setInterval(() => { api.mode().catch(() => {}) }, 4000)
    return () => clearInterval(id)
  }, [online])

  if (online) return null
  const slow = offlineReason() === 'slow'
  return (
    <div style={{
      background: '#3a2a12', border: '1px solid #7a5a1e', color: '#f0c274',
      borderRadius: 6, padding: '8px 12px', marginBottom: 12, fontSize: 13,
      display: 'flex', alignItems: 'center', gap: 10,
    }}>
      <span style={{ fontSize: 15 }}>◍</span>
      {slow ? (
        <span>
          <strong>Backend is answering slowly.</strong>{' '}
          It is up, but a request took longer than 20 seconds — usually the
          database is busy with a tick or a job. This page keeps retrying and
          will refill by itself; nothing has been lost. If it stays like this
          for minutes, the Data tab's "who holds the lock" panel says what is
          holding it.
        </span>
      ) : (
        <span>
          <strong>Backend not answering.</strong>{' '}
          It restarts on its own in a few seconds — this page is retrying and will
          refill by itself. Nothing has been lost.
        </span>
      )}
    </div>
  )
}

const ORDER_KEY = 'tc.navOrder.v1'
const PAGE_KEY = 'tc.page.v1'

/* Come back to the tab you left.
 *
 * On a phone, switching apps and coming back re-mounts the whole page, and
 * starting from `overview` every time means you lose your place every time you
 * answer a message. Every other app on the phone remembers; this one should
 * too. Kept in localStorage rather than the URL because the app has no router
 * and the tunnel address is pasted around -- a URL with a page in it would send
 * someone else to a tab they did not ask for. */
function loadPage() {
  try {
    const saved = localStorage.getItem(PAGE_KEY)
    // A page that no longer exists (renamed, removed) must not leave the app
    // blank: fall back rather than trust what is stored.
    return PAGES.some((p) => p[0] === saved) ? saved : 'overview'
  } catch { return 'overview' }
}

function loadOrder() {
  try {
    const saved = JSON.parse(localStorage.getItem(ORDER_KEY) || 'null')
    if (!Array.isArray(saved)) return PAGES.map((p) => p[0])
    const known = PAGES.map((p) => p[0])
    // keep saved order, append anything new, drop anything removed
    return [...saved.filter((id) => known.includes(id)),
            ...known.filter((id) => !saved.includes(id))]
  } catch { return PAGES.map((p) => p[0]) }
}

/* Shown when the backend refuses this device — and it says WHICH refusal.
 *
 * The happy path can still fail on a phone: an installed home-screen app has
 * its own storage, so a token stored in Safari is not there; clearing site data
 * wipes it; and a rotated token invalidates it. Any of those turn the whole app
 * into a spinner with an unhelpful error.
 *
 * But the screen used to give the same advice for a refusal no token can cure.
 * A backend started without phone access answers 403 to every outside caller
 * and never looks at a token, so "paste the token" sends you round in a circle:
 * paste a perfectly valid token, press save, the page reloads, the box is empty
 * again, and nothing anywhere says the token was never the problem.
 *
 * And pressing save told you nothing either — it stored whatever was typed and
 * reloaded on the spot, so right and wrong looked identical. Now the token is
 * checked against the backend BEFORE anything is stored, and the button says
 * what happened. */
function TokenGate() {
  const [reason, setReason] = React.useState(needsToken() ? (denyReason() || 'token') : '')
  const [value, setValue] = React.useState('')
  const [state, setState] = React.useState('')
  React.useEffect(() => onTokenNeeded((r) => { setReason(r); setState('') }), [])
  if (!reason) return null

  if (reason === 'closed') {
    return (
      <div className="card" style={{ margin: '10px 0', borderColor: 'var(--warning)' }}>
        <b>Phone access is off on the desktop</b>
        <div className="mut" style={{ fontSize: 12, marginTop: 6 }}>
          The backend is running but bound to this Mac only, so it is refusing
          every request from outside — a token will not help, and there is
          nothing to fix on this phone.
          <div style={{ marginTop: 6 }}>
            On the Mac, start it with the <b>Phone</b> button in Control Deck
            (or <code>bash run.sh --phone</code>), then reload here.
          </div>
        </div>
        <button style={{ marginTop: 10 }} onClick={() => window.location.reload()}>
          reload
        </button>
      </div>
    )
  }

  const busy = state === 'checking'
  const MSG = {
    checking: ['checking with the backend…', 'var(--mut)'],
    ok: ['accepted — reloading', 'var(--success)'],
    rejected: ['that token was not accepted — check for a missing character', 'var(--error)'],
    empty: ['paste the token first', 'var(--error)'],
    unreachable: ['could not reach the backend to check it', 'var(--error)'],
  }
  const note = MSG[state]

  async function save() {
    setState('checking')
    const r = await verifyToken(value)
    if (r === 'closed') { setReason('closed'); return }
    setState(r)
    if (r === 'ok') {
      setAccessToken(value)
      setTimeout(() => window.location.reload(), 700)   // long enough to read it
    }
  }

  return (
    <div className="card" style={{ margin: '10px 0', borderColor: 'var(--warning)' }}>
      <b>This device needs the access token</b>
      <div className="mut" style={{ fontSize: 12, margin: '6px 0' }}>
        It is shown on the Setup page on your Mac under “on your phone”. You only
        do this once per device.
      </div>
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <input value={value} onChange={(e) => { setValue(e.target.value.trim()); setState('') }}
               placeholder="paste the token" disabled={busy}
               autoCapitalize="off" autoCorrect="off" spellCheck={false}
               style={{ flex: '1 1 220px' }} />
        <button className="primary" onClick={save} disabled={busy || state === 'ok'}>
          {busy ? 'checking…' : state === 'ok' ? 'accepted' : 'check and save'}
        </button>
      </div>
      {note && (
        <div style={{ fontSize: 12, marginTop: 8, color: note[1] }}>{note[0]}</div>
      )}
    </div>
  )
}


/* PHONE NAVIGATION.
 *
 * The desktop sidebar has eighteen tabs. On a phone they were a horizontally
 * scrolling strip, which technically contained every page and in practice hid
 * all but the first two — there is nothing on screen telling you the row slides,
 * so the app looked like it only had Overview and Setup.
 *
 * A button that says where you are and opens the whole list is unambiguous. No
 * hidden gesture, every page one tap away, and the list is a grid so eighteen
 * entries fit without scrolling. */
function PhoneNav({ pages, page, setPage }) {
  const [open, setOpen] = React.useState(false)
  const current = pages.find((p) => p.id === page)
  return (
    <div className="phone-nav">
      <button className="phone-nav-toggle" onClick={() => setOpen(true)}>
        <span className="phone-nav-bars">☰</span>
        <span className="phone-nav-current">{current ? current.label : 'Menu'}</span>
      </button>
      {open && (
        <div className="phone-nav-sheet" onClick={() => setOpen(false)}>
          <div className="phone-nav-list" onClick={(e) => e.stopPropagation()}>
            <div className="phone-nav-head">
              <b>TradeCrypto</b>
              <button onClick={() => setOpen(false)}>close</button>
            </div>
            <div className="phone-nav-grid">
              {pages.map((p) => (
                <button key={p.id}
                        className={p.id === page ? 'active' : ''}
                        onClick={() => { setPage(p.id); setOpen(false) }}>
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [page, _setPage] = useState(loadPage)
  const setPage = React.useCallback((id) => {
    _setPage(id)
    try { localStorage.setItem(PAGE_KEY, id) } catch { /* private mode */ }
  }, [])
  const [model, setModel] = useState(null)
  const [order, setOrder] = useState(loadOrder)
  const [dragId, setDragId] = useState(null)

  const save = (next) => {
    setOrder(next)
    try { localStorage.setItem(ORDER_KEY, JSON.stringify(next)) } catch { /* private mode */ }
  }
  const onDrop = (targetId) => {
    if (!dragId || dragId === targetId) return
    const next = order.filter((x) => x !== dragId)
    next.splice(next.indexOf(targetId), 0, dragId)
    save(next)
    setDragId(null)
  }

  const byId = Object.fromEntries(PAGES.map(([id, label, C]) => [id, { label, C }]))
  const Page = (byId[page] || byId.overview).C
  return (
    // The whole shell is wrapped, not just the page area. A new person meeting
    // eighteen tabs, a drag-to-reorder hint and a Paper/Advisory/Live switch
    // cannot tell a working program from a broken one -- the operator's words
    // were "this app is confusing and also the setup is overwhelming". So on the
    // very first run the gate renders ONE question and nothing else; from the
    // second run onward it passes everything through untouched.
    <FirstRunGate>
    <div className="app">
      <aside className="sidebar">
        <PhoneNav pages={order.filter((id) => byId[id]).map((id) => ({ id, label: byId[id].label }))}
                  page={page} setPage={setPage} />
        <div className="brand">TradeCrypto<small>drag tabs to reorder</small></div>
        <nav className="nav">
          {order.map((id) => byId[id] && (
            <button key={id} className={`${page === id ? 'active' : ''} ${dragId === id ? 'dragging' : ''}`}
                    draggable
                    onDragStart={() => setDragId(id)}
                    onDragEnd={() => setDragId(null)}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={() => onDrop(id)}
                    onClick={() => setPage(id)}>
              <span className="grip">⠿</span>{byId[id].label}
            </button>
          ))}
        </nav>
        <button className="why" style={{ width: '100%', marginTop: 6 }}
                onClick={() => save(PAGES.map((p) => p[0]))}>reset order</button>
        <ModeSwitch />
      </aside>
      <main className="main">
        <ConnectionBanner />
        <TokenGate />
        {/* Scoped to the page area so the sidebar always survives a crash and
            there is always a way out. resetKey={page} clears the error when you
            navigate, so a broken page never becomes a dead end. */}
        <ErrorBoundary page={page} resetKey={page}>
          <Page onModel={setModel} />
        </ErrorBoundary>
      </main>
      <ModelDrawer name={model} onClose={() => setModel(null)} />
      {/* One chart host for the whole app: any symbol on any page opens here,
          above every panel, and there is only ever one of them open. */}
      <SymbolChartHost />
    </div>
    </FirstRunGate>
  )
}
